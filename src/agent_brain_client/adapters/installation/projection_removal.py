"""Remove an explicitly supplied owned projection; retain original queues and snapshots."""

from pathlib import Path
from agent_brain_client.adapters.installation import lifecycle
from agent_brain_client.adapters.storage.host_files import host_plan, host_apply, host_restore
from agent_brain_client.adapters.platforms.filesystem import cache_locked
from agent_brain_client.domain import contract


def prepare(session):
    counts = lifecycle.queue_counts(session)
    if any(counts[state] for state in ("pending", "conflict", "blocked")):
        raise ValueError("Unconfirmed writes remain in another registered Harness projection")
    if not session.mapping.transaction_settled():
        raise ValueError("Resolve the registered Harness mapping transaction first")
    owned = session.mapping.owned_snapshot()
    payload = {session.mapping._path(name): session.mapping.detached_bytes(name, session.mapping._raw(name))
               for name in owned["files"]}
    payload[session.cache / "owned.json"] = contract.encode(dict(owned, files={}))
    payload[session.cache / "active.json"] = None
    return payload


def remove(session):
    """Idempotent removal with a bounded, identity checked projection journal."""
    file = session.cache / "projection-removal.json"
    with (cache_locked(session.cache / ".content-runtime.lock", timeout=0),
          cache_locked(session.cache / "content.lock", timeout=0)):
        record = session._load(file.name)
        if record is not None and record.get("phase") == "complete" and session.mapping.owned_snapshot()["files"]:
            # A later verified mapping is a new installation generation. Keep
            # the completed receipt instead of treating it as current removal.
            if not session.mapping.transaction_settled() or session._load("active.json") is None:
                raise ValueError("Reinstalled projection requires a verified snapshot")
            from agent_brain_client.adapters.storage.content_limits import sha
            session._save("projection-removal-history/" + sha(contract.encode(record)) + ".json", record)
            record = None
        if record is not None:
            if record.get("binding") != session.binding or record.get("policy_sha") != session.policy_sha:
                raise ValueError("Retained projection removal identity changed")
            rows = record["host"]
            scope = {str(session.cache / "owned.json"), str(session.cache / "active.json")}
            for name in rows:
                if name not in scope:
                    logical = session.mapping.logical_name(Path(name))
                    if session.mapping._path(logical) != Path(name):
                        raise ValueError("Retained projection removal scope changed")
            if not scope <= set(rows):
                raise ValueError("Retained projection removal is incomplete")
            if record.get("phase") == "applying":
                host_restore(rows, lambda: session._save(file.name, record))
            elif record.get("phase") != "complete":
                raise ValueError("Unknown retained projection removal phase")
            if record.get("phase") == "complete":
                from agent_brain_client.adapters.storage.host_files import host_row
                if any(host_row(name, row)[2] != host_row(name, row)[1] for name, row in rows.items()):
                    raise ValueError("Removed Harness projection changed; preserve it")
                return
        payload = prepare(session)
        record = {"schema": 1, "binding": session.binding, "policy_sha": session.policy_sha,
                  "phase": "applying", "host": host_plan(payload, private_paths=(session.cache / "owned.json",))}
        session._save(file.name, record)
        try:
            host_apply(record["host"], lambda: session._save(file.name, record))
            record["phase"] = "complete"
            session._save(file.name, record)
        except Exception:
            host_restore(record["host"], lambda: session._save(file.name, record))
            raise
        session.mapping._prune_skill_directories({})
