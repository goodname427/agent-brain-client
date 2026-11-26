"""Construct isolated event queues from enrolled device metadata, without native mappers."""

from pathlib import Path
from agent_brain_client.adapters.storage import registration, metadata
from agent_brain_client.adapters.storage.content import FileEventQueue, operation_stage
from agent_brain_client.adapters.storage.workspace_identity import workspace_project
from agent_brain_client.adapters.platforms.filesystem import cache_directory
from agent_brain_client.application.memory.l0 import event_source_project
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
        queue = FileEventQueue(home, cache, policy, client, binding=binding,
            protected=[home / name for name in (".codex", ".agents", ".codebuddy", ".config/agent-brain-client/content")])
        queue.capture_workspace_project = lambda: workspace_project(local)
        client.event_source_project = lambda request: event_source_project(queue, request)
        origin_file = metadata.canonical(queue.cache / "origin.json")
        if not origin_file.exists():
            # Queue discovery relies on this record even if Onboarding was the
            # first caller and no native Hook has run in this origin.
            metadata.write(origin_file, local, cache=True)
        return client, queue
