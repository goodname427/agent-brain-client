"""Host-neutral private cache storage and durable event queue.

Bindings are supplied by hosts and remain byte-identical across upgrades.
No native configuration, native-source inventory or mapping is loaded here.
"""

from agent_brain_client.domain.content_policy import validate_policy
from agent_brain_client.domain.errors import CacheBlocked, CollectionCapacityExceeded


class NoNativeSources:
    def _assert_sources_shareable(self, keys):
        if keys:
            raise CacheBlocked()


import re

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path

import agent_brain_client.domain.contract as contract
from agent_brain_client.adapters.storage.host_files import atomic
from agent_brain_client.adapters.platforms.filesystem import (
    require_platform,
    private_mode,
    private_directory,
    sync_directory,
)
import agent_brain_client.adapters.platforms.filesystem as brain_lock
from agent_brain_client.application.queue.outbox import DurableOutbox

cache_mode = getattr(brain_lock, "cache_mode", private_mode)
cache_directory = getattr(brain_lock, "cache_directory", private_directory)
locked = getattr(brain_lock, "cache_locked", brain_lock.locked)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


_operation_diagnostic: ContextVar[dict[str, str] | None] = ContextVar(
    "content_operation_diagnostic", default=None
)


@contextmanager
def operation_diagnostics():
    """One call's path-free progress; never retain requests or exception text."""
    diagnostic = {"stage": "adapter-invoke"}
    token = _operation_diagnostic.set(diagnostic)
    try:
        yield diagnostic
    finally:
        _operation_diagnostic.reset(token)


@contextmanager
def operation_stage(stage):
    diagnostic = _operation_diagnostic.get()
    previous = diagnostic["stage"] if diagnostic is not None else "adapter-invoke"
    if diagnostic is not None:
        diagnostic["stage"] = stage
    try:
        yield
    except Exception:
        raise  # Retain the innermost failing stage until call exit.
    else:
        if diagnostic is not None:
            diagnostic["stage"] = previous


@contextmanager
def operation_lock(path, stage):
    with ExitStack() as stack:
        with operation_stage(stage):
            handle = stack.enter_context(locked(path))
        yield handle


class FileContentOutbox(DurableOutbox):
    _outbox_lock = staticmethod(operation_lock)
    _outbox_stage = staticmethod(operation_stage)

    @staticmethod
    def _outbox_locked(*args, **kwargs):
        return locked(*args, **kwargs)

    def __init__(self, home, cache, policy, client, *, binding, protected=()):
        self.platform = require_platform(
            "cache-storage", (getattr(client, "grant", None) or {}).get("platform")
        )
        if Path(home).is_symlink() or Path(cache).is_symlink():
            raise CacheBlocked()
        self.home, self.cache = Path(home).resolve(), Path(cache).resolve()
        self.sources = NoNativeSources()
        self.client, self.policy, self.binding = client, policy, binding
        self.policy_sha = sha(contract.encode({"policy": policy, "binding": binding}))
        self.queue_sha = sha(
            contract.encode(
                {
                    "policy_sha": self.policy_sha,
                    "service": getattr(
                        client, "queue_identity", getattr(client, "grant", None)
                    ),
                }
            )
        )
        validate_policy(policy, getattr(client, "grant", None))
        if self.home == self.cache or self.cache in self.home.parents:
            raise CacheBlocked()
        if any(
            self.cache == Path(path) or Path(path) in self.cache.parents
            for path in protected
        ):
            raise CacheBlocked()
        self._safe(self.home, self.home)
        self._safe(self.cache, self.cache)
        cache_directory(self.cache)
        cache_mode(self.cache, directory=True, blocked=CacheBlocked)
        sync_directory(self.cache.parent)

    def _cache_bytes(self, name):
        return 1048576

    def _safe(self, path, base):
        require_platform("cache-storage", self.platform)
        if path != base and base not in path.parents:
            raise CacheBlocked()
        for item in [path, *path.parents]:
            if item.is_symlink():
                raise CacheBlocked()
            if (
                os.name == "nt"
                and item.exists()
                and getattr(item.lstat(), "st_file_attributes", 0) & 0x400
            ):
                raise CacheBlocked()
            if item == base:
                break

    def _allows(self, kind, identity):
        return contract.allows(self.policy["ids"], kind, identity)

    def methods(self):
        grant = getattr(
            self.client,
            "grant",
            {"scopes": ["read", "write"], "artifact_types": self.policy["kinds"]},
        )
        methods = (
            contract.methods(grant["scopes"])
            if set(self.policy["kinds"]) <= set(grant["artifact_types"])
            else set()
        )
        if not set(self.policy["kinds"]) & {"rule", "skill"}:
            methods.discard("content_set_enabled")
        return (
            methods
            if "memory" in self.policy["kinds"]
            else methods - {"memory_query", "memory_event_submit"}
        )

    def _require_storage(self):
        require_platform("cache-storage", self.platform)

    def _queued(self):
        require_platform("cache-storage", self.platform)
        directory = self.cache / "outbox"
        self._safe(directory, self.cache)
        if directory.exists():
            cache_mode(directory, directory=True, blocked=CacheBlocked)
        paths = list(directory.iterdir()) if directory.exists() else []
        if len(paths) > 2048:
            raise CacheBlocked()
        rows = []
        for path in paths:
            self._safe(path, self.cache)
            # Preserve temporary evidence left by killed atomic saves; replay only
            # renamed request files so a lost confirmation remains recoverable.
            if re.fullmatch(r"\.client-native-[A-Za-z0-9_-]+", path.name):
                if not path.is_file() or path.stat().st_size > 1048576:
                    raise CacheBlocked()
                cache_mode(path, blocked=CacheBlocked)
                continue
            if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}\.json", path.name):
                raise CacheBlocked()
            row = self._load("outbox/" + path.name)
            if (
                not isinstance(row, dict)
                or set(row)
                != {
                    "schema",
                    "policy_sha",
                    "created",
                    "request",
                    "request_sha",
                    "state",
                    "response",
                    "remote",
                    "reconciles",
                    "resolution_request_id",
                }
                or row["schema"] != 1
                or row["policy_sha"] != self.queue_sha
                or type(row["created"]) is not int
                or row["state"]
                not in ("pending", "confirmed", "conflict", "blocked", "reconciled")
            ):
                raise CacheBlocked()
            self._check_write(row["request"])
            if (
                row["request"]["request_id"] + ".json" != path.name
                or sha(contract.encode(row["request"])) != row["request_sha"]
            ):
                raise CacheBlocked()
            rows.append(row)
        if len(rows) > 1024:
            raise CacheBlocked()
        return sorted(
            rows, key=lambda row: (row["created"], row["request"]["request_id"])
        )

    def _load(self, name, default=None):
        require_platform("cache-storage", self.platform)
        path = self.cache / name
        self._safe(path, self.cache)
        if not path.exists():
            return default
        limit = self._cache_bytes(name)
        if not path.is_file() or path.stat().st_size > limit:
            raise CacheBlocked()
        cache_mode(path, blocked=CacheBlocked)
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, name, value):
        require_platform("cache-storage", self.platform)
        raw = contract.encode(value)
        if len(raw) > self._cache_bytes(name):
            raise CollectionCapacityExceeded()
        path = self.cache / name
        self._safe(path, self.cache)
        atomic(path, raw, 0o600, cache=True)
        for directory in [path.parent, *path.parent.parents]:
            sync_directory(directory)
            if directory == self.cache:
                break


class FileEventQueue(FileContentOutbox):
    """Event-only host queue; cannot inventory or map another host's files."""

    def methods(self):
        return super().methods() & {"memory_event_submit"}

    def _assert_sources_shareable(self, source_keys):
        if source_keys:
            raise CacheBlocked()

    def invoke(self, request, reconciles=None, *, event_gate=None):
        result = self.invoke_write(request, reconciles, event_gate=event_gate)
        if result.get("local_refresh", {}).get("status") == "event-disabled":
            return result
        refresh = (
            "event-only"
            if result.get("local_queue", {}).get("state") == "confirmed"
            else "content-local-changes-retained"
        )
        return dict(
            result,
            local_refresh={
                "status": refresh,
                "native_changed": False,
                "model_loaded": "unknown",
            },
        )
