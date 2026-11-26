"""Keep source evidence alongside an immutable L0 outbox request."""

from agent_brain_client.domain.events import event_identifier, validate_workspace_project
from agent_brain_client.domain.scopes import digest


def _source_name(request):
    key = request["request_id"]
    event_identifier(key)
    return "event-sources/" + key + ".json"


def _source_fingerprint(request):
    event = request["params"]["event"]
    return digest({key: value for key, value in event.items() if key != "occurred_at"})


def _source(queue, request):
    name = _source_name(request)
    saved = queue._load(name)
    if saved is not None:
        if (
            not isinstance(saved, dict)
            or set(saved) != {"event_sha256", "project"}
            or saved["event_sha256"] != _source_fingerprint(request)
        ):
            raise ValueError("L0 event source changed")
        validate_workspace_project(saved["project"])
    return name, saved


def stage_source_project(queue, request, project):
    name, saved = _source(queue, request)
    if saved is None:
        validate_workspace_project(project)
        queue._save(name, {
            "event_sha256": _source_fingerprint(request),
            "project": project,
        })


def event_source_project(queue, request):
    _, saved = _source(queue, request)
    return None if saved is None else saved["project"]
