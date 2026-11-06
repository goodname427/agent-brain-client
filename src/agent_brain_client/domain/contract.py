"""domain / contract: one owner for each contract and rule."""

import hashlib
import re
from agent_brain_client.domain.operations import FIELDS
from agent_brain_client.domain.operations import HOOK_METHODS
from agent_brain_client.domain.authorization import OWNER_SCOPE
from agent_brain_client.domain.operations import SCOPES
from agent_brain_client.domain.content import TYPES
from agent_brain_client.domain.operations import WRITES
from agent_brain_client.domain.authorization import allows
from agent_brain_client.domain.recall import core_recall
from agent_brain_client.domain.receipts import core_receipt
from agent_brain_client.domain.validation import encode
from agent_brain_client.domain.content import envelope
from agent_brain_client.domain.events import event
from agent_brain_client.domain.events import event_identifier
from agent_brain_client.domain.recall import exact_capture_response
from agent_brain_client.domain.privacy import explicit_sharing_restriction
from agent_brain_client.domain.recall import global_recall_params
from agent_brain_client.domain.recall import global_recall_result
from agent_brain_client.domain.authorization import grant_ids
from agent_brain_client.domain.authorization import mcp_methods
from agent_brain_client.domain.authorization import methods
from agent_brain_client.domain.authorization import normalized_scopes
from agent_brain_client.domain.content import path
from agent_brain_client.domain.authorization import permits_scope
from agent_brain_client.domain.authorization import personal_owner
from agent_brain_client.domain.privacy import private_recall_text
from agent_brain_client.domain.validation import query_time
from agent_brain_client.domain.recall import recall_options
from agent_brain_client.domain.recall import recall_origin
from agent_brain_client.domain.content import record
from agent_brain_client.domain.sources import review_memory_sources
from agent_brain_client.domain.authorization import scope_subset
from agent_brain_client.domain.content import skill_frontmatter
from agent_brain_client.domain.content import skill_path
from agent_brain_client.domain.validation import strings
from agent_brain_client.domain.validation import text
from agent_brain_client.domain.recall import validate_capture_selection
from agent_brain_client.domain.recall import verified_memory_sources


def validate(method, params):
    if method == "memory_query" and isinstance(params, dict) and "recall" in params:
        recall_options(params["recall"])
        params = {k: v for k, v in params.items() if k != "recall"}
    if (
        method == "content_put"
        and isinstance(params, dict)
        and "source_context" in params
    ):
        context = params["source_context"]
        if (
            params.get("kind") != "memory"
            or not isinstance(context, dict)
            or set(context) != {"origin", "session_id"}
        ):
            raise ValueError("Memory source context required")
        if context["origin"] is not None:
            recall_origin(context["origin"])
        if context["session_id"] is not None:
            event_identifier(context["session_id"])
        params = {k: v for k, v in params.items() if k != "source_context"}
    if method == "content_catalog" and isinstance(params, dict) and "cursor" in params:
        if not isinstance(params["cursor"], str) or not re.fullmatch(
            r"[a-f0-9]{64}:[0-9]{1,20}", params["cursor"]
        ):
            raise ValueError("Invalid catalog cursor")
        params = {k: v for k, v in params.items() if k != "cursor"}
    if (
        method not in FIELDS
        or not isinstance(params, dict)
        or set(params)
        not in (
            FIELDS[method],
            FIELDS[method] | {"context"}
            if method == "memory_query"
            else FIELDS[method],
            FIELDS[method] | {"context", "selection"}
            if method == "memory_query"
            else FIELDS[method],
            FIELDS[method] | {"context", "source_sessions"}
            if method == "memory_query"
            else FIELDS[method],
            FIELDS[method] | {"context", "source_scope"}
            if method == "memory_query"
            else FIELDS[method],
        )
    ):
        raise ValueError("Invalid content request")
    if method in (
        "memory_status",
        "memory_resume",
        "memory_stop",
        "memory_revoke",
        "memory_sync",
    ):
        for item in params.values():
            event_identifier(item)
        return
    if method == "memory_query":
        if "source_scope" in params and params["source_scope"] != "approved-project":
            raise ValueError("Approved project source required")
        if (
            params["kind"] != "memory"
            or not isinstance(params["query"], str)
            or len(params["query"]) > 1024
            or type(params["limit"]) is not int
            or not 1 <= params["limit"] <= 2**31 - 1
        ):
            raise ValueError("Bounded Memory query required")
        since, until = query_time(params["since"]), query_time(params["until"])
        if since is not None and until is not None and since > until:
            raise ValueError("Ordered query range required")
        if "context" in params:
            context = params["context"]
            if (
                not isinstance(context, dict)
                or set(context) != {"project_id", "session_id", "cross_session"}
                or context["cross_session"] is not True
            ):
                raise ValueError("Explicit cross-session recall required")
            for field in ("project_id", "session_id"):
                event_identifier(context[field])
            if not params["query"].strip() or since is not None or until is not None:
                raise ValueError("Core recall requires text and no legacy time range")
        if "selection" in params:
            validate_capture_selection(params["selection"])
            if (
                params["limit"] != 3
                or params["query"] != "capture-batches-exact-v1"
                or params["context"]["session_id"]
                == params["selection"]["source_session_id"]
            ):
                raise ValueError("Separate exact three-layer recall required")
        if "source_sessions" in params:
            sources = params["source_sessions"]
            if (
                not isinstance(sources, list)
                or not 1 <= len(sources) <= 16
                or any(not isinstance(s, str) for s in sources)
                or len(set(sources)) != len(sources)
                or params["context"]["session_id"] in sources
            ):
                raise ValueError("Distinct finite recall sources required")
            for source in sources:
                event_identifier(source)
        return
    if method == "content_catalog":
        kinds = params["kinds"]
        if (
            not isinstance(kinds, list)
            or not kinds
            or any(not isinstance(k, str) or k not in TYPES for k in kinds)
            or len(set(kinds)) != len(kinds)
        ):
            raise ValueError("Unsupported kinds")
        return
    if (
        not isinstance(params["kind"], str)
        or params["kind"] not in TYPES
        or not isinstance(params["id"], str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", params["id"])
    ):
        raise ValueError("Unsupported content identity")
    if method == "memory_event_submit":
        if params["kind"] != "memory":
            raise ValueError("Memory event required")
        event(params["event"])
        if params["id"] != params["event"]["event_id"]:
            raise ValueError("Event identity mismatch")
    if method in ("content_set_enabled", "content_put", "content_remove") and (
        not isinstance(params["expected_revision"], str)
        or not re.fullmatch(r"missing|[a-f0-9]{64}", params["expected_revision"])
    ):
        raise ValueError("Read revision required")
    if method == "content_set_enabled":
        if (
            params["kind"] not in ("rule", "skill")
            or type(params["enabled"]) is not bool
            or params["expected_revision"] == "missing"
        ):
            raise ValueError("Existing Rule/Skill and explicit enabled state required")
    if method == "content_put":
        record(params["kind"], params["record"], params["id"])


def catalog_page(value, cursor=None, scope=None):
    """One revision-bound page; the count of stored records is unrestricted."""
    snapshot = hashlib.sha256(
        encode([value["commit"], value["artifacts"], scope])
    ).hexdigest()
    start = 0
    if cursor is not None:
        match = re.fullmatch(r"([a-f0-9]{64}):([0-9]{1,20})", cursor)
        if match is None or match[1] != snapshot:
            raise ValueError("Catalog changed; restart without cursor")
        start = int(match[2])
        if start >= len(value["artifacts"]):
            raise ValueError("Invalid catalog offset")
    end = start + 256
    return dict(
        value,
        artifacts=value["artifacts"][start:end],
        **(
            {"next_cursor": snapshot + ":" + str(end)}
            if end < len(value["artifacts"])
            else {}
        ),
    )


def validate_catalog(value):
    if (
        not isinstance(value, dict)
        or set(value)
        not in (
            {"content_api_version", "commit", "artifacts"},
            {"content_api_version", "commit", "artifacts", "next_cursor"},
        )
        or value["content_api_version"] != 1
        or not isinstance(value["commit"], str)
        or not re.fullmatch("[a-f0-9]{40}", value["commit"])
        or not isinstance(value["artifacts"], list)
        or len(value["artifacts"]) > 256
    ):
        raise ValueError("Invalid catalog page")
    if "next_cursor" in value and (
        not isinstance(value["next_cursor"], str)
        or not re.fullmatch(r"[a-f0-9]{64}:[0-9]{1,20}", value["next_cursor"])
    ):
        raise ValueError("Invalid catalog continuation")


__all__ = [
    "FIELDS",
    "HOOK_METHODS",
    "OWNER_SCOPE",
    "SCOPES",
    "TYPES",
    "WRITES",
    "allows",
    "catalog_page",
    "core_recall",
    "core_receipt",
    "encode",
    "envelope",
    "event",
    "event_identifier",
    "exact_capture_response",
    "explicit_sharing_restriction",
    "global_recall_params",
    "global_recall_result",
    "grant_ids",
    "mcp_methods",
    "methods",
    "normalized_scopes",
    "path",
    "permits_scope",
    "personal_owner",
    "private_recall_text",
    "query_time",
    "recall_options",
    "recall_origin",
    "record",
    "review_memory_sources",
    "scope_subset",
    "skill_frontmatter",
    "skill_path",
    "strings",
    "text",
    "validate",
    "validate_capture_selection",
    "validate_catalog",
    "verified_memory_sources",
]
