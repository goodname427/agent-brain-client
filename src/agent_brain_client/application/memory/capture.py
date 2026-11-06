"""Capture one normalized turn through the bound durable queue."""

from agent_brain_client.domain.turns import normalized_turn


def submit_turn(
    adapter, scope, turn, phase, content, *, enabled, lock, clock=None, source_gate=None
):
    """Stage a normalized prompt/final pair and submit via the original outbox.

    The host validates native scope and recursion before this call. Stable scope,
    turn and key keep existing request IDs and schema-1 queue bytes compatible.
    """
    from datetime import datetime, timezone

    if not enabled():
        return {"status": "disabled", "data_transferred": False}
    key = normalized_turn(scope, turn, phase, content)
    session = scope["session_id"]
    name = "hook-turns/" + key + ".json"
    with lock(adapter.cache / "event-hook.lock"):
        if not enabled():
            return {"status": "disabled", "data_transferred": False}
        row = adapter._load(name)
        if phase == "prompt":
            if row is not None:
                if (
                    row["scope"] != scope
                    or row["turn_id"] != turn
                    or row["prompt"] != content
                ):
                    raise ValueError("Native turn changed; preserve evidence")
                return {"status": "staged", "data_transferred": False}
            directory = adapter.cache / "hook-turns"
            adapter._safe(directory, adapter.cache)
            if directory.exists() and len(list(directory.iterdir())) >= 1024:
                raise ValueError("Hook staging capacity reached")
            now = clock() if clock else datetime.now(timezone.utc)
            stamp = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            row = {
                "schema": 1,
                "scope": scope,
                "turn_id": turn,
                "prompt": content,
                "occurred_at": stamp,
                "final": None,
            }
            adapter._save(name, row)
            return {"status": "staged", "data_transferred": False}
        if row is None:
            return {"status": "skipped-no-prompt", "data_transferred": False}
        if (
            set(row) != {"schema", "scope", "turn_id", "prompt", "occurred_at", "final"}
            or row["schema"] != 1
            or row["scope"] != scope
            or row["turn_id"] != turn
            or row["final"] not in (None, content)
        ):
            raise ValueError("Native staging changed")
        row["final"] = content
        adapter._save(name, row)
        event = {
            "contract": "agentbrain.event.v1",
            "event_id": "evt-" + key[:56],
            "project_id": scope["project_id"],
            "session_id": session,
            "occurred_at": row["occurred_at"],
            "messages": [
                {"role": "user", "content": row["prompt"]},
                {"role": "assistant", "content": content},
            ],
        }
        request = {
            "request_id": "agentbrain_event_" + key,
            "method": "memory_event_submit",
            "params": {"kind": "memory", "id": event["event_id"], "event": event},
        }
        if not enabled():
            return {"status": "disabled", "data_transferred": False}
        # Refresh/write transport can hold content.lock beyond the hook wait.
        # Persist this exact event first under the short append lock. Automatic
        # drains skip it; invoke still owns content.lock before any transfer and
        # rechecks the same immutable request plus the current off gate.
        adapter._enqueue(request)
        return adapter.invoke(
            request,
            event_gate=lambda: enabled() and (source_gate is None or source_gate()),
        )
