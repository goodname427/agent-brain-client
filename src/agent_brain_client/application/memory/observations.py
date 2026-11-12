"""Persist observed messages identically for every Harness, without network I/O."""

from datetime import datetime, timezone
import json
from agent_brain_client.domain import contract, privacy
from agent_brain_client.domain.scopes import digest, restricted
from agent_brain_client.domain.errors import CacheBlocked


def _finish_staging(queue, name, saved):
    if (not isinstance(saved, dict) or set(saved) != {"schema", "requests", "requests_sha256"}
            or saved["schema"] != 1 or not isinstance(saved["requests"], list)
            or not saved["requests"] or digest(saved["requests"]) != saved["requests_sha256"]):
        raise ValueError("Invalid durable observation")
    for request in saved["requests"]:
        if (not isinstance(request, dict) or set(request) != {"request_id", "method", "params"}
                or request.get("method") != "memory_event_submit" or not isinstance(request["params"], dict)):
            raise ValueError("Observed events required")
        contract.validate(request["method"], request["params"])
        if any(privacy.private_content(m["content"]) or restricted(m["content"])
               for m in request["params"]["event"]["messages"]):
            raise ValueError("Private observation preserved locally")
    rows = [queue._enqueue(request) for request in saved["requests"]]
    # The content is removable only after every immutable outbox row exists.
    queue._discard_staging(name)
    return rows


def resume_staging(queue):
    """Recreate a missing tail from durable observations, without host history."""
    with queue._outbox_lock(queue.cache / "observed-messages.lock", "capture-stage-lock"):
        for name in queue._staged_observations():
            try:
                _finish_staging(queue, name, queue._load(name))
            except CacheBlocked:
                # Existing pending rows can drain and free bounded active capacity.
                return True
            except ValueError as error:
                # Preserve invalid evidence separately; one damaged operation
                # must not starve unrelated, validated pending events.
                queue._review_staging(name, type(error).__name__)
    return False


def _chunks(content):
    start = 0
    while start < len(content):
        end = min(start + 4096, len(content))
        # A new transport message must not turn a body example into a directive
        # merely by placing it at the beginning of a chunk.
        while end < len(content) and end > start and (
                privacy.private_content(content[end:end + 4096])
                or restricted(content[end:end + 4096])):
            end -= 1
        if end == start:
            raise ValueError("Observation cannot be split without changing privacy")
        yield content[start:end]
        start = end


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
        contract.text(message["content"], 4 * 1048576)
    if any(privacy.private_content(m["content"]) or restricted(m["content"]) for m in messages):
        return {"status": "excluded-local-only-or-sensitive", "data_transferred": False}
    batches = []
    batch: list[dict[str, str]] = []
    for message in messages:
        for content in _chunks(message["content"]):
            item = {"role": role, "content": content}
            contract.text(item["content"], 32768)
            if len(json.dumps([*batch, item], ensure_ascii=False).encode()) > 64000 or len(batch) == 100:
                batches.append(batch)
                batch = []
            batch.append(item)
    if batch:
        batches.append(batch)
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
        pending = "observed-staging/" + operation_key + ".json"
        saved = queue._load(pending)
        if saved is not None:
            rows = _finish_staging(queue, pending, saved)
            return _result(rows)
        requests = []
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
            requests.append(request)
        saved = {"schema": 1, "requests": requests, "requests_sha256": digest(requests)}
        # Atomic full observation precedes the first enqueue. A process may die
        # after any batch; the supervisor needs no input replay to finish it.
        queue._save(pending, saved)
        rows = _finish_staging(queue, pending, saved)
    return _result(rows)


def _result(rows):
    return {"status": "staged", "data_transferred": False,
            "local_queue": {"saved": True, "state": "confirmed" if all(r["state"] == "confirmed" for r in rows) else "pending"},
            "request_ids": [r["request"]["request_id"] for r in rows], "native_event_batches": len(rows)}
