"""domain / events: one owner for each contract and rule."""

import hashlib
import re
from agent_brain_client.domain.validation import encode
from agent_brain_client.domain.validation import query_time
from agent_brain_client.domain.validation import text

NATIVE_MEMORY_CONTRACT = "agentbrain.native-memory.v1"
MAX_NATIVE_MEMORY_BYTES = 131072

PROJECT_ORIGIN_CAPABILITY = "project-origin-v1"
WORKSPACE_ORIGIN_CAPABILITY = "workspace-origin-v1"


def validate_workspace(workspace):
    """A host label and device-local ID, with an independently verified portable ID."""
    if not isinstance(workspace, dict) or set(workspace) not in (
        {"id", "display_name"}, {"id", "display_name", "portable_id"},
    ):
        raise ValueError("Exact workspace metadata required")
    local, name = workspace["id"], workspace["display_name"]
    if (
        not isinstance(local, str)
        or not re.fullmatch(r"workspace-local-sha256:[a-f0-9]{64}", local)
        or not isinstance(name, str) or not 1 <= len(name) <= 128
        or name != name.strip() or name in (".", "..")
        or any(char in "/\\" or not char.isprintable() for char in name)
    ):
        raise ValueError("Path-free workspace label and local identity required")
    if "portable_id" in workspace:
        portable = workspace["portable_id"]
        if not isinstance(portable, str) or not re.fullmatch(r"(git|svn|p4)-sha256:[a-f0-9]{64}", portable):
            raise ValueError("Verified portable workspace identity required")


def validate_workspace_project(project):
    if not isinstance(project, dict) or set(project) != {"id", "repository"}:
        raise ValueError("Exact workspace project identity required")
    repository = project["repository"]
    if (
        not isinstance(repository, str)
        or len(repository) > 512
        or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*(?:/[A-Za-z0-9_.-]+){2,}", repository)
        or any(part in (".", "..") for part in repository.split("/"))
        or repository.startswith("github.com/") and repository != repository.lower()
        or project["id"] != "git-sha256:" + hashlib.sha256(repository.encode()).hexdigest()
    ):
        raise ValueError("Normalized Git repository and matching digest required")


def l0_request(event, request_id):
    return {
        "request_id": request_id, "method": "memory_event_submit",
        "params": {"kind": "memory", "id": event["event_id"], "event": event},
    }


def event_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value
    ):
        raise ValueError("Bounded event identity required")


def event(value):
    if isinstance(value, dict) and value.get("contract") == NATIVE_MEMORY_CONTRACT:
        native_memory_event(value)
        return
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "contract",
            "event_id",
            "project_id",
            "session_id",
            "occurred_at",
            "messages",
        }
        or value["contract"] != "agentbrain.event.v1"
    ):
        raise ValueError("Exact event required")
    for name in ("event_id", "project_id", "session_id"):
        event_identifier(value[name])
    stamp = value["occurred_at"]
    if not isinstance(stamp, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z", stamp
    ):
        raise ValueError("UTC event time required")
    query_time(stamp)
    messages = value["messages"]
    if not isinstance(messages, list) or not 1 <= len(messages) <= 100:
        raise ValueError("Bounded event messages required")
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message["role"] not in ("user", "assistant")
        ):
            raise ValueError("Event message required")
        text(message["content"], 32768)
        if len(message["content"].encode("utf-16-le")) // 2 > 8192:
            raise ValueError("Event message exceeds bound")
    if len(encode(value)) > 120000:
        raise ValueError("Event exceeds bound")


def native_source_identity(source_id, fingerprint):
    if (
        not isinstance(source_id, str)
        or not re.fullmatch(r"(codex|codebuddy)-memory:[a-f0-9]{64}", source_id)
        or not isinstance(fingerprint, str)
        or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
    ):
        raise ValueError("Exact native source identity required")


def native_memory_content(body):
    """Original UTF-8 text, with no rewriting before its source fingerprint."""
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Nonempty native source text required")
    data = body.encode("utf-8")
    if len(data) > MAX_NATIVE_MEMORY_BYTES:
        raise ValueError("Native source content exceeds bound")
    return data


def native_memory_event(value):
    # The queue pins the route and source coordinates locally. The global wire
    # removes them from the event and lets the Server bind the approved route.
    if set(value) != {
        "contract", "event_id", "project_id", "session_id", "occurred_at",
        "source_id", "source_fingerprint", "content",
    }:
        raise ValueError("Exact native Memory event required")
    for name in ("event_id", "project_id", "session_id"):
        event_identifier(value[name])
    native_source_identity(value["source_id"], value["source_fingerprint"])
    stamp = value["occurred_at"]
    if not isinstance(stamp, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z", stamp
    ):
        raise ValueError("UTC source time required")
    query_time(stamp)
    data = native_memory_content(value["content"])
    if hashlib.sha256(data).hexdigest() != value["source_fingerprint"]:
        raise ValueError("Native source content changed")
