"""Keep source evidence alongside an immutable L0 outbox request."""

from agent_brain_client.domain.events import event_identifier, validate_workspace_project
from agent_brain_client.domain.scopes import digest


def _source_name(request):
    key = request["request_id"]
    event_identifier(key)
    return "event-sources/" + key + ".json"


def _source(queue, request):
    name = _source_name(request)
    saved = queue._load(name)
    if saved is not None:
        if (
            not isinstance(saved, dict)
            or set(saved) != {"event_sha256", "project", "send_project"}
            or saved["event_sha256"] != digest(request["params"]["event"]["messages"])
            or saved["send_project"] is not None and type(saved["send_project"]) is not bool
        ):
            raise ValueError("L0 event source changed")
        validate_workspace_project(saved["project"])
    return name, saved


def stage_source_project(queue, request, project):
    name, saved = _source(queue, request)
    if saved is None:
        validate_workspace_project(project)
        queue._save(name, {
            "event_sha256": digest(request["params"]["event"]["messages"]),
            "project": project, "send_project": None,
        })


def event_source_project(queue, request, supported):
    name, saved = _source(queue, request)
    if saved is None:
        return None
    if saved["send_project"] is None:
        # The same request ID must keep its original wire shape on retry,
        # including after an ambiguous response or a Server version change.
        saved["send_project"] = supported()
        queue._save(name, saved)
    return saved["project"] if saved["send_project"] else None
