"""Construct isolated event queues from enrolled device metadata, without native mappers."""

from pathlib import Path
from agent_brain_client.adapters.storage import registration, metadata
from agent_brain_client.adapters.storage.content import FileEventQueue, operation_stage
from agent_brain_client.adapters.platforms.filesystem import cache_directory
from agent_brain_client.domain.scopes import digest
from agent_brain_client.domain.errors import CacheBlocked


def open_queue(value, local, harness, *, services):
    if harness not in ("codex", "codebuddy"):
        raise ValueError("Explicit source Harness required")
    home = Path(value["device_home"]).resolve()
    client = services.CollectionClient(value, local)
    if harness == "codebuddy":
        client.queue_identity = dict(client.queue_identity, host_adapter=harness)
    _, enrolled = registration.registration(home)
    runtime = enrolled["runtime"]
    policy = metadata.private_json(runtime["content_policy"], runtime["content_policy_sha256"])
    native = Path(runtime["codex_home"]).expanduser()
    if not native.is_absolute() or native.is_symlink():
        raise CacheBlocked()
    # These persisted queue bindings predate the split and must not change.
    binding = {"device_home": str(home), "codex_home": str(native.resolve())}
    root = home / ".config/agent-brain-client" / ("global-memory" if harness == "codex" else harness)
    cache = root / "outboxes" / digest(local)
    with operation_stage("global-cache-open"):
        cache_directory(cache)
        return client, FileEventQueue(home, cache, policy, client, binding=binding,
            protected=[home / name for name in (".codex", ".agents", ".codebuddy", ".config/agent-brain-client/content")])
