"""Device background policy and coalescing of finite attempts."""

import math
import time
from datetime import datetime, timezone
from pathlib import Path

from agent_brain_client.adapters.platforms.filesystem import (
    LockTimeout, cache_directory, cache_locked, locked, private_directory, private_mode,
)
from agent_brain_client.adapters.storage.content_limits import SNAPSHOT_BYTES
from agent_brain_client.adapters.storage.metadata import canonical, private_json, write
from agent_brain_client.application.control import check_request


def directory(home):
    return canonical(Path(home) / ".config/agent-brain-client/background")


def policy_directory(home):
    return canonical(directory(home).with_name("background-policy"))


def read(path, *, cache=False):
    path = canonical(path)
    if not path.exists():
        return None
    try:
        return private_json(path, cache=cache, max_bytes=4096)
    except FileNotFoundError:
        return None


def update_policy(home):
    value = read(policy_directory(home) / "auto-update.json")
    if value is None:
        return {"schema": 1, "enabled": True, "paused_until": None}
    until = value.get("paused_until") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict) or set(value) != {"schema", "enabled", "paused_until"}
        or value["schema"] != 1 or type(value["enabled"]) is not bool
        or (until is not None and (type(until) not in (int, float) or not math.isfinite(until) or until <= 0))
    ):
        raise ValueError("Invalid automatic update policy")
    return value


def policy_state(value, now):
    if not value["enabled"]:
        return "disabled"
    if value["paused_until"] is not None and now < value["paused_until"]:
        return "paused"
    return "enabled"


def manage_policy(home, action=None, until=None, *, clock=time.time):
    now = clock()
    if action is not None:
        root = policy_directory(home)
        private_directory(root)
        private_mode(root, directory=True, owner=True)
        with locked(canonical(root / "policy.lock")):
            current = update_policy(home)
            if action in ("enable", "disable"):
                current = dict(current, enabled=action == "enable", paused_until=None)
            elif action == "pause-day":
                current = dict(current, paused_until=now + 86400)
            elif action == "pause":
                timestamp = datetime.fromisoformat(until.replace("Z", "+00:00"))
                if timestamp.utcoffset() is None or timestamp.timestamp() <= now:
                    raise ValueError("A future time with an offset is required")
                current = dict(current, paused_until=timestamp.timestamp())
            else:
                raise ValueError("Unknown update policy operation")
            write(root / "auto-update.json", current)
    else:
        current = update_policy(home)
    until = current["paused_until"]
    return {
        "status": "client-auto-update-policy",
        "policy": policy_state(current, now),
        "enabled": current["enabled"],
        "paused_until": datetime.fromtimestamp(until, timezone.utc).isoformat() if until is not None else None,
        "last_check": read(directory(home) / "update-attempt.json", cache=True),
    }


def run_due(path, identity, interval, perform, *, success, clock=time.time):
    """Lease one task across launchers. Attempts survive a stopped process."""
    cache_directory(path.parent)
    try:
        with cache_locked(canonical(path.with_suffix(".lock")), timeout=0):
            previous = read(path, cache=True) or {}
            now = clock()
            elapsed = now - previous.get("attempted_at", 0)
            if previous.get("identity") == identity and 0 <= elapsed < interval:
                return {"status": "background-not-due", "retry_after": interval - elapsed}
            state = {"identity": identity, "attempted_at": now, "status": "attempting",
                     "succeeded_at": previous.get("succeeded_at")
                     if previous.get("identity") == identity else None}
            write(path, state, cache=True)
            try:
                status = perform()
            except Exception:
                status = "background-failed"
            state["status"] = status
            if status in success:
                state["succeeded_at"] = clock()
            write(path, state, cache=True)
            return {"status": status}
    except LockTimeout:
        return {"status": "background-busy", "retry_after": 5}


def shared_catalog(home, identity, fetch, *, interval, clock, release):
    """Coalesce a catalog scan only across identical service and policy bindings."""
    root = cache_directory(directory(home))
    path = root / ("catalog-" + identity + ".json")
    limit = SNAPSHOT_BYTES * 3
    with cache_locked(canonical(path.with_suffix(".lock")), timeout=10, cancel_check=check_request):
        now = clock()
        if path.exists():
            saved = private_json(path, cache=True, max_bytes=limit)
            if (saved.get("identity") == identity and saved.get("release") == release
                    and 0 <= now - saved["checked_at"] < interval):
                return saved["catalog"]
        catalog = fetch()
        write(path, {"identity": identity, "release": release, "checked_at": now, "catalog": catalog},
              cache=True, max_bytes=limit)
        return catalog
