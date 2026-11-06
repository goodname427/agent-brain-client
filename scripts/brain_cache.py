"""Host-neutral private cache storage and durable event queue.

Bindings are supplied by hosts and remain byte-identical across upgrades.
No native configuration, native-source inventory or mapping is loaded here.
"""

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path

import brain_content as contract
from brain_host_files import atomic
from brain_lock import require_platform, private_mode, private_directory, sync_directory
import brain_lock
from brain_memory import CacheBlocked, ContentOutbox

cache_mode = getattr(brain_lock, "cache_mode", private_mode)
cache_directory = getattr(brain_lock, "cache_directory", private_directory)
locked = getattr(brain_lock, "cache_locked", brain_lock.locked)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class CollectionCapacityExceeded(CacheBlocked):
    pass


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


def validate_policy(policy, grant=None):
    if (
        not isinstance(policy, dict)
        or set(policy) != {"schema", "adapter", "kinds", "ids", "dependencies"}
        or policy["schema"] != 1
        or policy["adapter"] != "codex"
    ):
        raise CacheBlocked()
    contract.validate("content_catalog", {"kinds": policy["kinds"]})
    if not isinstance(policy["ids"], dict) or set(policy["ids"]) != set(
        policy["kinds"]
    ):
        raise CacheBlocked()
    try:
        contract.grant_ids(policy["kinds"], policy["ids"])
    except ValueError:
        raise CacheBlocked()
    if not isinstance(policy["dependencies"], dict) or set(policy["dependencies"]) != {
        "commands",
        "python_modules",
    }:
        raise CacheBlocked()
    contract.strings(policy["dependencies"]["commands"])
    contract.strings(policy["dependencies"]["python_modules"])
    if grant and (
        "read" not in grant["scopes"]
        or set(policy["kinds"]) - set(grant["artifact_types"])
    ):
        raise CacheBlocked()
    if grant:
        try:
            contract.grant_ids(grant["artifact_types"], grant.get("ids"))
        except ValueError:
            raise CacheBlocked()
        if not contract.scope_subset(policy["ids"], grant["ids"]):
            raise CacheBlocked()


class StoredContentOutbox(ContentOutbox):
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


class EventQueue(StoredContentOutbox):
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
