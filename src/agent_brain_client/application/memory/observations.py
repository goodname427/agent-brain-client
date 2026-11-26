"""Persist observed messages identically for every Harness, without network I/O."""

from datetime import datetime, timezone
import json
from agent_brain_client.domain import contract
from agent_brain_client.application.memory.l0 import stage_workspace_source
from agent_brain_client.domain.events import l0_request
from agent_brain_client.domain.scopes import digest
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


def _operation_key(project_id, session_id, operation_id, phase):
    return digest(["observed-v1", project_id, session_id, operation_id, phase])


def _batch_key(project_id, session_id, operation_id, phase, index):
    return digest(["observed-v1", project_id, session_id, operation_id, phase, index])


def prompt_fingerprint(queue, *, project_id, session_id, operation_id):
    """Fingerprint of an existing replayable prompt, including partial staging."""
    key = _operation_key(project_id, session_id, operation_id, "prompt")
    marker = queue._load("observed-operations/" + key + ".json")
    if marker is None:
        return None
    batch = _batch_key(project_id, session_id, operation_id, "prompt", 0)
    if (queue._load("outbox/observed_event_" + batch + ".json") is not None
            or queue._load("observed-staging/" + key + ".json") is not None):
        return marker["content_sha256"]
    return None


def stage_messages(queue, *, project_id, session_id, operation_id, phase, messages, occurred_at=None,
                   workspace=None):
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
    operation_key = _operation_key(project_id, session_id, operation_id, phase)
    fingerprint = digest(messages)
    # Freeze timestamps once under the short enqueue lock; never wait on transport.
    with queue._outbox_lock(queue.cache / "observed-messages.lock", "capture-stage-lock"):
        marker = "observed-operations/" + operation_key + ".json"
        previous = queue._load(marker)
        if previous is not None and previous.get("content_sha256") != fingerprint:
            raise ValueError("Observed operation changed; preserve queued event")
        pending = "observed-staging/" + operation_key + ".json"
        saved = queue._load(pending)
        if saved is not None:
            rows = _finish_staging(queue, pending, saved)
            return _result(rows)
        # A completed staging journal leaves every original request in
        # the outbox. Reuse those requests even if a prior version split
        # the same message differently.
        rows = []
        while True:
            key = _batch_key(project_id, session_id, operation_id, phase, len(rows))
            row = queue._load("outbox/observed_event_" + key + ".json")
            if row is None:
                break
            rows.append(row)
        if rows:
            captured = "".join(m["content"] for row in rows
                               for m in row["request"]["params"]["event"]["messages"])
            if captured != "".join(m["content"] for m in messages):
                raise ValueError("Observed operation changed; preserve queued event")
            if previous is None:
                queue._save(marker, {"schema": 1, "content_sha256": fingerprint})
            return _result(rows)

        batches = []
        batch: list[dict[str, str]] = []
        for message in messages:
            body = message["content"]
            for start in range(0, len(body), 4096):
                content = body[start:start + 4096]
                item = {"role": role, "content": content}
                contract.text(item["content"], 32768)
                if len(json.dumps([*batch, item], ensure_ascii=False).encode()) > 64000 or len(batch) == 100:
                    batches.append(batch)
                    batch = []
                batch.append(item)
        if batch:
            batches.append(batch)
        requests = []
        for index, batch in enumerate(batches):
            key = _batch_key(project_id, session_id, operation_id, phase, index)
            rid = "observed_event_" + key
            event = {
                "contract": "agentbrain.event.v1", "event_id": "evt-observed-" + key[:48],
                "project_id": project_id, "session_id": session_id,
                "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "messages": batch,
            }
            request = l0_request(event, rid)
            contract.validate(request["method"], request["params"])
            requests.append(request)
        saved = {"schema": 1, "requests": requests, "requests_sha256": digest(requests)}
        if previous is None:
            queue._save(marker, {"schema": 1, "content_sha256": fingerprint})
        # Atomic full observation precedes the first enqueue. A process may die
        # after any batch; the supervisor needs no input replay to finish it.
        if workspace is not None:
            for request in requests:
                stage_workspace_source(queue, request, workspace)
        queue._save(pending, saved)
        rows = _finish_staging(queue, pending, saved)
    return _result(rows)


def _result(rows):
    return {"status": "staged", "data_transferred": False,
            "local_queue": {"saved": True, "state": "confirmed" if all(r["state"] == "confirmed" for r in rows) else "pending"},
            "request_ids": [r["request"]["request_id"] for r in rows], "native_event_batches": len(rows)}
