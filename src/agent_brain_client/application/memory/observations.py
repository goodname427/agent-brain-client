"""Persist observed messages identically for every Harness, without network I/O."""

from datetime import datetime, timezone
import json
from agent_brain_client.domain import contract, privacy
from agent_brain_client.domain.scopes import digest, restricted


def stage_messages(queue, *, project_id, session_id, operation_id, phase, messages, occurred_at=None):
    contract.event_identifier(session_id)
    contract.event_identifier(project_id)
    contract.event_identifier(operation_id)
    if phase not in ("prompt", "assistant") or not messages:
        raise ValueError("Observed message phase required")
    role = "user" if phase == "prompt" else "assistant"
    for message in messages:
        if set(message) != {"role", "content"} or message["role"] != role:
            raise ValueError("Exact observed role required")
        if not isinstance(message["content"], str) or not message["content"]:
            raise ValueError("Observed text required")
    if any(privacy.private_content(m["content"]) or restricted(m["content"]) for m in messages):
        return {"status": "excluded-local-only-or-sensitive", "data_transferred": False}
    batches = []
    batch: list[dict[str, str]] = []
    for message in messages:
        for start in range(0, len(message["content"]), 4096):
            item = {"role": role, "content": message["content"][start:start + 4096]}
            contract.text(item["content"], 32768)
            if len(json.dumps([*batch, item], ensure_ascii=False).encode()) > 64000 or len(batch) == 100:
                batches.append(batch)
                batch = []
            batch.append(item)
    if batch:
        batches.append(batch)
    rows = []
    operation_key = digest(["observed-v1", project_id, session_id, operation_id, phase])
    fingerprint = digest(messages)
    # Freeze timestamps once under the short enqueue lock; never wait on transport.
    with queue._outbox_lock(queue.cache / "observed-messages.lock", "capture-stage-lock"):
        marker = "observed-operations/" + operation_key + ".json"
        previous = queue._load(marker)
        if previous is not None and previous.get("content_sha256") != fingerprint:
            raise ValueError("Observed operation changed; preserve queued event")
        if previous is None:
            queue._save(marker, {"schema": 1, "content_sha256": fingerprint})
        for index, batch in enumerate(batches):
            key = digest(["observed-v1", project_id, session_id, operation_id, phase, index])
            rid = "observed_event_" + key
            old = queue._load("outbox/" + rid + ".json")
            if old is not None:
                request = old["request"]
                if request["params"]["event"]["messages"] != batch:
                    raise ValueError("Observed operation changed; preserve queued event")
            else:
                event = {
                    "contract": "agentbrain.event.v1", "event_id": "evt-observed-" + key[:48],
                    "project_id": project_id, "session_id": session_id,
                    "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "messages": batch,
                }
                request = {"request_id": rid, "method": "memory_event_submit",
                           "params": {"kind": "memory", "id": event["event_id"], "event": event}}
                contract.validate(request["method"], request["params"])
            rows.append(queue._enqueue(request))
    return {"status": "staged", "data_transferred": False,
            "local_queue": {"saved": True, "state": "confirmed" if all(r["state"] == "confirmed" for r in rows) else "pending"},
            "request_ids": [r["request"]["request_id"] for r in rows], "native_event_batches": len(rows)}
