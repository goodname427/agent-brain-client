"""Keep source evidence alongside an immutable L0 outbox request."""

from agent_brain_client.domain.events import event_identifier, validate_workspace, validate_workspace_project
from agent_brain_client.domain.errors import CacheBlocked
from agent_brain_client.domain.scopes import digest


def _source_name(request, directory):
    key = request["request_id"]
    event_identifier(key)
    return directory + "/" + key + ".json"


def _source_fingerprint(request):
    event = request["params"]["event"]
    return digest({key: value for key, value in event.items() if key != "occurred_at"})


def _source(queue, request, directory, key, validate):
    name = _source_name(request, directory)
    saved = queue._load(name)
    if saved is not None:
        if (
            not isinstance(saved, dict)
            or set(saved) != {"event_sha256", key}
            or saved["event_sha256"] != _source_fingerprint(request)
        ):
            raise ValueError("L0 event source changed")
        validate(saved[key])
    return name, saved


def stage_source_project(queue, request, project):
    name, saved = _source(queue, request, "event-sources", "project", validate_workspace_project)
    if saved is None:
        validate_workspace_project(project)
        queue._save(name, {
            "event_sha256": _source_fingerprint(request),
            "project": project,
        })


def event_source_project(queue, request):
    _, saved = _source(queue, request, "event-sources", "project", validate_workspace_project)
    return None if saved is None else saved["project"]


def stage_workspace_source(queue, request, workspace):
    if workspace is None:
        return
    # A distinct sidecar lets an older delivery worker keep reading its exact
    # event-sources schema. Neither file enters the immutable outbox request.
    try:
        name, saved = _source(queue, request, "event-workspaces", "workspace", validate_workspace)
        value = workspace.source(getattr(queue, "workspace_device", None))
        if saved is None and value is not None:
            saved = {"event_sha256": _source_fingerprint(request), "workspace": value}
            queue._save(name, saved)
        git_project = workspace.git_project
        if git_project is not None and (
            saved is None or saved["workspace"].get("portable_id") == git_project["id"]
        ):
            stage_source_project(queue, request, git_project)
    except (CacheBlocked, OSError, ValueError):
        pass  # Optional provenance does not prevent staging the original event.


def event_workspace_source(queue, request):
    _, saved = _source(queue, request, "event-workspaces", "workspace", validate_workspace)
    return None if saved is None else saved["workspace"]
