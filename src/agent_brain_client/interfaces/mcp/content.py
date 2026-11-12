"""Content MCP interface for scoped memory, Rule and Skill operations."""


import sys
from pathlib import Path

if __package__ in (None, ""):
    _ENTRY_ROOT = Path(__file__).resolve().parents[4]
    sys.path[:0] = [str(_ENTRY_ROOT / "src"), str(_ENTRY_ROOT)]


from agent_brain_client.interfaces.mcp.framing import (
    ProtocolError,
    keys,
    pairs,
    ContentParameterError,
    ContentToolUnavailable,
)


from agent_brain_client.domain.tool_contract import TOOL_CONTRACT_VERSION
from agent_brain_client.application.tools.catalog import (
    RECALL_EVIDENCE_GUIDANCE,
    tool_definitions,
    LOCAL_TOOLS,
    memory_sources_authorized,
    tool_names,
    enabled_tools,
)
from agent_brain_client.adapters.storage.pinned import pinned

from agent_brain_client.interfaces.mcp.server import serve
from agent_brain_client.domain.errors import CacheBlocked
from agent_brain_client.adapters.storage.reinstallation import require_memory_verified
from agent_brain_client.application.installation.fresh import (
    FreshMemoryVerificationRequired,
)

from agent_brain_client.adapters.storage.origins import discover as discover_origin


from agent_brain_client.application.memory.guidance import MEMORY_GUIDANCE

import argparse
import hashlib
import json
import os
import re
import ssl
from urllib.error import HTTPError
import uuid
from agent_brain_client.interfaces.mcp.topics import Server
from agent_brain_client.adapters.storage.content_session import (
    ContentSession,
    contract,
    ROOT as CLIENT_ROOT,
)
from agent_brain_client.adapters.storage.content import operation_diagnostics
from agent_brain_client.domain.content_policy import validate_policy
from agent_brain_client.adapters.platforms.macos.component import ComponentClient
from agent_brain_client.adapters.platforms.filesystem import (
    LockTimeout,
    PlatformUnsupported,
    private_mode,
)
import agent_brain_client.adapters.platforms.filesystem as brain_lock

cache_locked = brain_lock.cache_locked
cache_directory = brain_lock.cache_directory
from agent_brain_client.application.control import RequestStopped
from agent_brain_client.adapters.packages.client import UPDATE_GUIDANCE


def worker_commit(client_version):
    """Observe this immutable package's receipt, never the current active pointer."""
    try:
        path = CLIENT_ROOT / "verified.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            return None
        receipt = json.loads(path.read_bytes())
        manifest = receipt["manifest"]
        commit = receipt["commit"]
        import re

        if (
            manifest["version"] != client_version
            or not isinstance(commit, str)
            or not re.fullmatch(r"[a-f0-9]{40}", commit)
        ):
            return None
        if (
            manifest["files"]["src/agent_brain_client/interfaces/mcp/content.py"]
            != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        ):
            return None
        return commit
    except Exception:
        return None


class ContentServer(Server):
    def __init__(self, adapter, client_version="0.2.0"):
        super().__init__(None, client_version)
        self.adapter = adapter
        self.running_worker_commit = worker_commit(client_version)

    @property
    def client_runtime(self):
        """Report this connection; local release inspection never fetches or pairs."""
        value = {
            "running_worker_version": self.client_version,
            "running_worker_commit": self.running_worker_commit,
            "service_mode": "approved-content",
            "tool_contract_version": TOOL_CONTRACT_VERSION,
            "offered_tools": tool_names(
                self.adapter.methods(), self._memory_source_tools_authorized()
            ),
            "host_tool_visibility": "unknown",
            "model_loaded": "unknown",
            "active_package_version": None,
            "active_package_commit": None,
            "reload_required": None,
            "reload_reason": "active-package-unobserved",
        }
        if not isinstance(getattr(self.adapter, "home", None), Path):
            return value
        try:
            from agent_brain_client.adapters.storage.private_files import (
                private_json,
            )
            from agent_brain_client.adapters.packages.client import ClientReleases

            meta = private_json(
                self.adapter.home / ".config/agent-brain-client/codex-mcp.json"
            )
            releases = ClientReleases(
                meta["release_policy"], meta["release_policy_sha256"], inspection=True
            )
            if (
                Path(releases.policy["runtime"]["device_home"]).resolve()
                != self.adapter.home
            ):
                return value
            active = releases.verified_active(serving=True)
            if active is None:
                return value
            value.update(
                active_package_version=active["version"],
                active_package_commit=active.get("commit"),
            )
            # A version mismatch proves this worker is old. Equality needs the
            # exact commit too; source checkouts / legacy receipts stay unknown.
            if active["version"] != self.client_version:
                value.update(
                    reload_required=True, reload_reason="running-worker-differs"
                )
            elif (
                self.running_worker_commit is not None
                and active.get("commit") is not None
            ):
                same = self.running_worker_commit == active["commit"]
                value.update(
                    reload_required=not same,
                    reload_reason="running-worker-current"
                    if same
                    else "running-worker-differs",
                )
            else:
                value["reload_reason"] = "running-worker-commit-unobserved"
        except Exception:
            pass  # Status failure must not block ordinary memory operations.
        return value

    def _memory_source_tools_authorized(self):
        policy = getattr(self.adapter, "policy", None)
        client = getattr(self.adapter, "client", None)
        grant = getattr(client, "grant", None)
        return memory_sources_authorized(policy, grant)

    def layered_recall(self, arguments):
        if "memory_query" not in tool_names(
            self.adapter.methods(), self._memory_source_tools_authorized()
        ):
            raise ContentToolUnavailable(
                "Layered recall requires the existing Memory read grant."
            )
        try:
            if not isinstance(arguments, dict):
                raise ValueError()
            arguments = dict(arguments)
            if (
                arguments.pop("kind", "memory") != "memory"
                or arguments.pop("since", None) is not None
                or arguments.pop("until", None) is not None
            ):
                raise ValueError()
            arguments.pop("request_id", None)
            if arguments.get("query", "") == "":
                arguments["query"] = "personal preferences and task progress"
            if "origin" not in arguments:
                arguments["origin"] = discover_origin()
            elif (
                isinstance(arguments["origin"], dict)
                and arguments["origin"].get("kind") == "project"
                and arguments["origin"].get("repository_root") is None
            ):
                arguments["origin"] = dict(
                    arguments["origin"],
                    repository_root=arguments["origin"].get("project_root"),
                )
            arguments.setdefault("session_id", "reader-" + uuid.uuid4().hex)
            params = dict({"limit": 5, "recall": {}}, **arguments)
            contract.global_recall_params(params)
        except (ValueError, TypeError):
            raise ContentParameterError(
                "Use a text query and optional integer limit. Origin and reader session default automatically."
            ) from None
        except (OSError, ImportError):
            raise ProtocolError(
                -32010,
                "Current directory metadata unavailable; supply origin explicitly.",
            ) from None
        rid = uuid.uuid4().hex
        try:
            result = self.adapter.recall(
                {"request_id": rid, "method": "memory_query", "params": params}
            )
        except RequestStopped:
            raise
        except Exception:
            result = {
                "request_id": rid,
                "error": {
                    "code": "layered_recall_unconfirmed",
                    "retryable": False,
                    "stage": "response-validation",
                },
                "persistence": {"durable": False},
            }
        result = dict(result, client_runtime=self.client_runtime)
        return {
            "isError": "error" in result,
            "structuredContent": result,
            "content": [
                {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
            ],
        }

    def invoke(self, name, arguments):
        if name == "memory_query":
            return self.layered_recall(arguments)
        try:
            if name == "content_queue_inspect":
                if name not in tool_names(
                    self.adapter.methods(), self._memory_source_tools_authorized()
                ):
                    raise ContentToolUnavailable(
                        "Queue inspection requires an existing content write grant."
                    )
                if (
                    not isinstance(arguments, dict)
                    or set(arguments) - {"request_id", "cursor"}
                    or len(arguments) > 1
                ):
                    raise ContentParameterError(
                        "Inspect a queue page with optional cursor, or one exact request_id."
                    )
                rid = arguments.get("request_id")
                cursor = arguments.get("cursor")
                if (
                    "request_id" in arguments
                    and (
                        not isinstance(rid, str)
                        or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", rid) is None
                    )
                    or "cursor" in arguments
                    and (
                        not isinstance(cursor, str)
                        or re.fullmatch(r"[a-f0-9]{64}:[0-9]{1,6}", cursor) is None
                    )
                ):
                    raise ContentParameterError(
                        "Use an exact retained request_id or the returned next_cursor."
                    )
                try:
                    result = self.adapter.inspect_queue(request_id=rid, cursor=cursor)
                except LockTimeout:
                    raise ProtocolError(
                        -32000,
                        "Local queue busy; retry inspection after the active operation finishes.",
                    ) from None
                except OSError:
                    raise ProtocolError(
                        -32010,
                        "Queue inspection unavailable; preserve retained requests and restore private cache access.",
                    ) from None
                return {
                    "isError": False,
                    "structuredContent": result,
                    "content": [
                        {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                    ],
                }
            if (
                name.startswith("content_sources_")
                and not self._memory_source_tools_authorized()
            ):
                raise ContentParameterError(
                    "Native Memory source tools are outside the granted Memory scope."
                )
            if name == "content_request_id":
                required = {"turn_sha256", "method", "kind", "id"}
                if not isinstance(arguments, dict) or set(arguments) != required:
                    raise ContentParameterError(
                        "Generate an idempotent request ID for one target and operation."
                    )
                if not isinstance(arguments["turn_sha256"], str) or not re.fullmatch(
                    r"[a-f0-9]{64}", arguments["turn_sha256"]
                ):
                    raise ContentParameterError(
                        "turn_sha256 must be the digest from the active Codex notify event."
                    )
                if arguments["method"] not in (
                    "content_put",
                    "content_remove",
                    "content_set_enabled",
                ):
                    raise ContentParameterError(
                        "Only content write request IDs are supported."
                    )
                contract.validate(
                    "content_read", {"kind": arguments["kind"], "id": arguments["id"]}
                )
                rid = (
                    "agentbrain_"
                    + hashlib.sha256(
                        contract.encode(
                            [
                                arguments["turn_sha256"],
                                arguments["method"],
                                arguments["kind"],
                                arguments["id"],
                            ]
                        )
                    ).hexdigest()
                )
                result = {
                    "request_id": rid,
                    "data_transferred": False,
                    "native_changed": False,
                }
                return {
                    "isError": False,
                    "structuredContent": result,
                    "content": [
                        {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                    ],
                }
            if name == "content_sources_inventory":
                if not isinstance(arguments, dict) or set(arguments) - {"cursor"}:
                    raise ContentParameterError(
                        "Inventory accepts only an optional cursor."
                    )
                inventory = self.adapter.sources.inventory_memory_sources()
                cursor = arguments.get("cursor")
                offset = 0
                if cursor is not None:
                    if (
                        not isinstance(cursor, str)
                        or len(cursor) > 96
                        or ":" not in cursor
                    ):
                        raise ContentParameterError(
                            "Invalid inventory cursor; restart enumeration."
                        )
                    snapshot, raw_offset = cursor.split(":", 1)
                    if (
                        len(snapshot) != 64
                        or any(char not in "0123456789abcdef" for char in snapshot)
                        or not raw_offset.isdecimal()
                        or len(raw_offset) > 6
                    ):
                        raise ContentParameterError(
                            "Invalid inventory cursor; restart enumeration."
                        )
                    if snapshot != inventory["cursor"]:
                        raise ContentParameterError(
                            "Inventory changed; restart enumeration from the first page."
                        )
                    offset = int(raw_offset)
                sources = inventory["sources"]
                page = sources[offset : offset + 64]
                next_offset = offset + len(page)
                result = {
                    "status": inventory["status"],
                    "counts": inventory["counts"],
                    "cursor": inventory["cursor"],
                    "sources": page,
                    "next_cursor": f"{inventory['cursor']}:{next_offset}"
                    if next_offset < len(sources)
                    else None,
                    "reason": inventory["reason"],
                    "data_transferred": False,
                    "account_complete": False,
                }
                return {
                    "isError": False,
                    "structuredContent": result,
                    "content": [
                        {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                    ],
                }
            if name == "content_sources_read":
                if not isinstance(arguments, dict) or set(arguments) != {
                    "source_id",
                    "source_fingerprint",
                }:
                    raise ContentParameterError(
                        "Read exactly one inventoried source by ID and fingerprint."
                    )
                result = self.adapter.sources.read_memory_source(
                    arguments["source_id"], arguments["source_fingerprint"]
                )
                return {
                    "isError": False,
                    "structuredContent": result,
                    "content": [
                        {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                    ],
                }
            if name == "content_sources_review":
                if not isinstance(arguments, dict) or set(arguments) != {
                    "source_id",
                    "source_fingerprint",
                    "decision",
                    "reason_code",
                }:
                    raise ContentParameterError(
                        "Review requires one exact source fingerprint, a decision, and a fixed reason code."
                    )
                result = self.adapter.sources.review_memory_source(
                    arguments["source_id"],
                    arguments["source_fingerprint"],
                    arguments["decision"],
                    arguments["reason_code"],
                )
                return {
                    "isError": False,
                    "structuredContent": result,
                    "content": [
                        {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                    ],
                }
            if name == "content_sources_stage":
                expected = {"source_id", "source_fingerprint", "id", "record"}
                if not isinstance(arguments, dict) or set(arguments) != expected:
                    raise ContentParameterError(
                        "Stage requires one locally read source and a normalized AgentBrain Memory record."
                    )
                result = self.adapter.sources.stage_memory_import(
                    arguments["source_id"],
                    arguments["source_fingerprint"],
                    "memory",
                    arguments["id"],
                    arguments["record"],
                )
                return {
                    "isError": False,
                    "structuredContent": result,
                    "content": [
                        {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                    ],
                }
            if name in contract.HOOK_METHODS or name not in self.adapter.methods():
                raise ContentToolUnavailable(
                    "Tool is not exposed by this worker. Inspect tools/list and the installed tool grant; retry the normal MCP connection if it failed to initialize. Tool absence alone does not prove a remote outage."
                )
            if not isinstance(arguments, dict):
                raise ContentParameterError("Content tool arguments must be an object.")
            required = contract.FIELDS[name] | (
                {"request_id"} if name in contract.WRITES else set()
            )
            allowed = (
                contract.FIELDS[name]
                | ({"cursor"} if name == "content_catalog" else set())
                | {"request_id"}
                | (
                    {"reconciles", "source_context"}
                    if name == "content_put"
                    else {"reconciles"}
                    if name == "content_set_enabled"
                    else set()
                )
            )
            if set(arguments) - allowed or required - set(arguments):
                raise ValueError()
            params = dict(arguments)
            reconciles = params.pop("reconciles", None)
            rid = params.pop("request_id", uuid.uuid4().hex)
            contract.validate(name, params)
            if (
                not isinstance(rid, str)
                or not 16 <= len(rid) <= 128
                or any(
                    c
                    not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                    for c in rid
                )
            ):
                raise ValueError()
        except ContentParameterError:
            raise
        except CacheBlocked:
            if name == "content_queue_inspect":
                raise ProtocolError(
                    -32010,
                    "Queue inspection unavailable; preserve retained requests and restore the original Client identity, policy and private cache access.",
                ) from None
            raise ProtocolError(
                -32010,
                "Native Memory source changed or is unavailable; refresh inventory before retrying.",
            ) from None
        except (ValueError, TypeError):
            if name == "content_queue_inspect":
                raise ContentParameterError(
                    "Invalid queue inspection arguments or queue changed; restart without cursor, then use the returned request_id or next_cursor."
                ) from None
            message = (
                "Invalid "
                + name
                + " arguments; use the exact fields and types in this tool input schema."
            )
            if name in contract.WRITES:
                message += " Writes require a stable request_id and the expected_revision obtained from content_read."
            raise ContentParameterError(message) from None
        with operation_diagnostics() as diagnostic:
            try:
                request = {"request_id": rid, "method": name, "params": params}
                if (
                    name == "content_put"
                    and params["kind"] == "memory"
                    and hasattr(self.adapter, "prepare_memory_write")
                ):
                    session = os.environ.get("CODEX_THREAD_ID")
                    if not isinstance(session, str) or not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", session
                    ):
                        session = None
                    try:
                        origin = discover_origin()
                    except OSError:
                        origin = None
                    request = self.adapter.prepare_memory_write(
                        request, {"origin": origin, "session_id": session}
                    )
                result = (
                    self.adapter.invoke(request, reconciles=reconciles)
                    if reconciles is not None
                    else self.adapter.invoke(request)
                )
            except RequestStopped:
                raise
            except Exception as error:
                # A busy local lock is transient. Keep the original CAS request
                # and ID for an explicit retry; do not replay anything here.
                busy = isinstance(error, LockTimeout)
                known = (
                    CacheBlocked,
                    LockTimeout,
                    OSError,
                    PermissionError,
                    FileNotFoundError,
                    FileExistsError,
                    NotADirectoryError,
                    IsADirectoryError,
                    TimeoutError,
                    ValueError,
                    TypeError,
                    KeyError,
                    RuntimeError,
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                    UnicodeEncodeError,
                )
                exception_type = (
                    type(error).__name__ if type(error) in known else "Exception"
                )
                result = {
                    "request_id": rid,
                    "error": {
                        "code": "content_busy" if busy else "content_adapter_blocked",
                        "retryable": busy,
                        "stage": diagnostic["stage"],
                        "exception_type": exception_type,
                    },
                    "persistence": {"durable": False},
                }
        result = dict(result, client_runtime=self.client_runtime)
        return {
            "isError": "error" in result,
            "structuredContent": result,
            "content": [
                {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
            ],
        }

    def handle(self, request):
        if (
            isinstance(request, dict)
            and request.get("method") == "tools/list"
            and self.state == "ready"
        ):
            keys(
                request,
                ("jsonrpc", "id", "method", "params"),
                ("jsonrpc", "id", "method"),
            )
            if request["jsonrpc"] != "2.0" or type(request["id"]) not in (str, int):
                raise ProtocolError(-32600, "Invalid JSON-RPC request.")
            params = request.get("params", {})
            if isinstance(params, dict) and "_meta" in params:
                if not isinstance(params["_meta"], dict):
                    raise ProtocolError(-32602, "Invalid MCP metadata.")
                params = {key: value for key, value in params.items() if key != "_meta"}
            keys(params, ("cursor",))
            if params.get("cursor") not in (None, ""):
                raise ProtocolError(-32602, "Unknown cursor.")
            return {
                "tools": tool_definitions(
                    self.adapter.methods(),
                    getattr(self.adapter, "policy", {}).get(
                        "kinds", ["memory", "rule", "skill"]
                    ),
                    self._memory_source_tools_authorized(),
                    self.client_version,
                    UPDATE_GUIDANCE,
                )
            }
        result = super().handle(request)
        if (
            isinstance(request, dict)
            and request.get("method") == "initialize"
            and isinstance(result, dict)
        ):
            mapping = self.adapter.refresh()
            result["content_mapping"] = mapping
            result["serverInfo"]["name"] = "agent-brain-content"
            result["instructions"] = (
                RECALL_EVIDENCE_GUIDANCE
                + MEMORY_GUIDANCE
                + "content_catalog lists authorized AgentBrain destination records only; it does not discover Harness-native sources. "
                + "Writes require the current expected_revision and stable request_id; retain exact request arguments on retries. "
                + "Pending/local-confirmed/remote-confirmed and durable fields distinguish client, Server and backup status. "
                + "Ordinary edits retain Rule/Skill enabled state; content_set_enabled requires an explicit user activation request. Refreshing mappings does not reload an already running model. "
                + "Only granted methods and IDs are exposed; pairing and publication remain separate. "
                + "Native mapping result: "
                + json.dumps(mapping, sort_keys=True)
                + ". "
                + UPDATE_GUIDANCE
            )
        return result


def self_check():
    class Adapter:
        def refresh(self):
            return {"status": "not-wired", "native_changed": False}

        def methods(self):
            return set(contract.FIELDS)

    server = ContentServer(Adapter())
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "synthetic-package-check", "version": "1"},
            },
        }
    )
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    names = [
        t["name"]
        for t in server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[
            "tools"
        ]
    ]
    assert len(names) == len(set(names))
    assert set(names) == set(
        tool_names(server.adapter.methods(), server._memory_source_tools_authorized())
    )
    assert not any(
        m in sys.modules for m in ("brain_core", "brain_runtime", "brain_cli")
    )
    return {
        "content_mcp_self_check": "passed",
        "core_imported": False,
        "native_changed": False,
        "keychain_accessed": False,
        "network_used": False,
    }


STARTUP_STAGES = {
    "arguments",
    "component-source-check",
    "content-policy-validation",
    "https-profile-load",
    "credential-source-validation",
    "device-binding-validation",
    "identity-activation",
    "ssh-profile-load",
    "signing-policy-validation",
    "device-component-load",
    "transport-policy-validation",
    "installation-check",
    "content-adapter-start",
    "import-plan-validation",
    "mcp-serve",
    "event-hook-policy-validation",
    "event-hook-input-read",
    "event-hook-payload-validation",
    "event-hook-run",
}


def emit_startup_failure(stage, error, stream=None):
    """Emit a bounded diagnostic; exception values and tracebacks are never serialized."""
    if stream is None:
        stream = sys.stderr
    if isinstance(error, ssl.SSLError):
        error_class = "SSLError"
        error_code = "startup_transport_error"
    elif isinstance(error, HTTPError):
        error_class = "HTTPError"
        error_code = "startup_transport_error"
    elif isinstance(error, OSError):
        error_class = "OSError"
        error_code = "startup_io_error"
    elif isinstance(error, ValueError):
        error_class = "ValueError"
        error_code = "startup_validation_error"
    elif isinstance(error, KeyError):
        error_class = "KeyError"
        error_code = "startup_validation_error"
    elif isinstance(error, TypeError):
        error_class = "TypeError"
        error_code = "startup_validation_error"
    elif isinstance(error, RuntimeError):
        error_class = "RuntimeError"
        error_code = "startup_blocked"
    elif type(error).__name__ in ("TimeoutExpired", "SSLError", "HTTPError"):
        error_class = type(error).__name__
        error_code = "startup_transport_error"
    else:
        error_class = "StartupError"
        error_code = "startup_blocked"
    safe_stage = stage if stage in STARTUP_STAGES else "startup"
    print(
        json.dumps(
            {"stage": safe_stage, "error_class": error_class, "error_code": error_code}
        ),
        file=stream,
    )
    return 1


class SafeArgumentParser(argparse.ArgumentParser):
    """Keep invalid argument values out of stderr."""

    def error(self, message):
        sys.exit(emit_startup_failure("arguments", ValueError("invalid_arguments")))


# Codex invokes notify once after a turn and passes the event as one JSON argv.
# An internal marker makes the queued capture turn a one-shot, not a loop.
_NOTIFY_CAPTURE_MARKER = "AGENTBRAIN_NOTIFY_CAPTURE:"
_NOTIFY_CAPTURE_INSTRUCTIONS = (
    "Review the immediately preceding completed Codex turn for user-stated preferences, "
    "facts, experiences, events, corrections, or short-term progress. Do not filter "
    "these out solely because they are ordinary, temporary, or not important. Use only "
    "direct user statements as evidence; ignore assistant "
    "guesses and tool output. Process only the supported, authorized Codex Memories "
    "scope exposed by content_sources_inventory/read/review/stage. Read every "
    "eligible source to completion in bounded pages; do not claim account-wide or "
    "session-history coverage. Do not count unread items as complete. Generalize "
    "ordinary user project experience safely for the shared personal Memory scope; "
    "exclude secrets, LocalOnly, confidential or customer-identifying content, and "
    "report unsafe scope as deferred. For memory writes use content_read/CAS and "
    "content_request_id with the supplied turn digest. Honor explicit forgetting "
    "with the existing authorized content_remove path. Do not read transcripts or "
    "store raw conversation text. Never claim durable save without the Server receipt."
)


def codex_notify_capture(event):
    """Validate the documented notify payload and prepare one capture message."""
    if not isinstance(event, dict) or event.get("type") != "agent-turn-complete":
        return None
    thread_id = event.get("thread-id")
    turn_id = event.get("turn-id")
    messages = event.get("input-messages")
    if (
        not isinstance(thread_id, str)
        or not thread_id
        or len(thread_id) > 128
        or not isinstance(turn_id, str)
        or not turn_id
        or len(turn_id) > 256
        or not isinstance(messages, list)
        or not messages
        or len(messages) > 128
    ):
        return None
    if any(not isinstance(item, str) or len(item) > 65536 for item in messages):
        return None
    if any(item.startswith(_NOTIFY_CAPTURE_MARKER) for item in messages):
        return None
    last = event.get("last-assistant-message", "")
    if not isinstance(last, str) or len(last) > 65536:
        return None
    turn_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    message = (
        _NOTIFY_CAPTURE_MARKER
        + turn_id
        + "\nTurn digest: "
        + turn_digest
        + "\n"
        + _NOTIFY_CAPTURE_INSTRUCTIONS
    )
    return {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "turn_sha256": turn_digest,
        "message": message,
        "input_message_count": len(messages),
    }


def installation_check(client, policy, client_version="0.2.0"):
    """Fixed read-only MCP/auth check. No content cache/native mapping is created."""
    validate_policy(policy, client.grant)

    class Adapter:
        def __init__(self):
            self.policy = policy

        def refresh(self):
            return {"status": "protocol-only", "native_changed": False}

        def methods(self):
            return set(enabled_tools(policy, client.grant)) - LOCAL_TOOLS

        def invoke(self, request):
            if request["method"] != "content_catalog":
                raise ValueError("Read-only check")
            return client.request(request)

    adapter = Adapter()
    server = ContentServer(adapter, client_version)
    first = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "codex-installation-check", "version": "1"},
            },
        }
    )
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    result = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "content_catalog",
                "arguments": {"kinds": policy["kinds"]},
            },
        }
    )
    tool_names = [tool["name"] for tool in tools["tools"]]
    expected_tools = set(enabled_tools(policy, client.grant))
    if (
        first["serverInfo"]["name"] != "agent-brain-content"
        or len(tool_names) != len(set(tool_names))
        or set(tool_names) != expected_tools
        or result["isError"]
    ):
        raise ValueError("Content protocol check failed")
    catalog = result["structuredContent"]["result"]
    import re

    contract.validate_catalog(catalog)
    for item in catalog["artifacts"]:
        contract.validate("content_read", {"kind": item["kind"], "id": item["id"]})
        if item["kind"] not in policy["kinds"] or not re.fullmatch(
            r"[a-f0-9]{64}", item.get("revision", "")
        ):
            raise ValueError("Invalid content catalog")
    return {
        "status": "content-native-protocol-checked",
        "protocol_checked": True,
        "authentication_checked": True,
        "tool_contract_version": TOOL_CONTRACT_VERSION,
        "tools": sorted(expected_tools),
        "native_changed": False,
        "model_loaded": "unknown",
    }


_STARTUP_STAGE = "arguments"


def main():
    global _STARTUP_STAGE
    _STARTUP_STAGE = "arguments"
    if sys.argv[1:] == ["--describe-tools"]:
        value = json.load(sys.stdin)
        print(
            json.dumps(
                {
                    "tools": enabled_tools(value["policy"], value["grant"]),
                    "version": TOOL_CONTRACT_VERSION,
                }
            )
        )
        return
    if sys.argv[1:2] == ["--event-hook-off"]:
        if len(sys.argv) != 3:
            raise ValueError("Exact off marker required")
        target = Path(sys.argv[2])
        if (
            not target.is_absolute()
            or target.is_symlink()
            or target.parent.resolve() != target.parent
            or not target.parent.is_dir()
        ):
            raise ValueError("Existing private off directory required")
        private_mode(target.parent, directory=True, owner=True)
        from agent_brain_client.adapters.storage.host_files import atomic

        atomic(target, b"capture disabled\n", 0o600)
        print("{}")
        return
    if sys.argv[1:] == ["--self-check"]:
        print(json.dumps(self_check()))
        return
    parser = SafeArgumentParser(description=__doc__)
    for name in ("content-policy", "content-policy-sha256", "device-home", "cache"):
        parser.add_argument("--" + name, required=True)
    for name in (
        "device-plan",
        "device-plan-sha256",
        "signing-policy",
        "signing-policy-sha256",
        "ssh-profile",
        "ssh-profile-sha256",
        "https-profile",
        "https-profile-sha256",
        "credential-source-sha256",
    ):
        parser.add_argument("--" + name)
    parser.add_argument("--harness", choices=("codex", "codebuddy"), required=True)
    parser.add_argument(
        "--codex-home",
        help="Approved actual Codex home; otherwise honor CODEX_HOME and preserve device-level skills.",
    )
    parser.add_argument(
        "--installation-check",
        action="store_true",
        help="Fixed read-only protocol/auth check; does not write cache or native files.",
    )
    parser.add_argument(
        "--mapping-refresh",
        action="store_true",
        help="Finite native mapping after installation; no source discovery or collection.",
    )
    parser.add_argument(
        "--event-hook",
        action="store_true",
        help="One finite authorized native event; no refresh, inventory or installer.",
    )
    parser.add_argument("--event-hook-policy")
    parser.add_argument("--event-hook-policy-sha256")
    parser.add_argument(
        "--import-plan",
        help="Explicit reviewed private normalized source plan; no native discovery.",
    )
    parser.add_argument(
        "--import-plan-sha256",
        help="Exact reviewed source-plan digest, required with --import-plan.",
    )
    parser.add_argument(
        "--component-source-sha256",
        help="Release policy pin for the separately approved signed component source.",
    )
    parser.add_argument(
        "--client-version",
        default="0.2.0",
        help="Verified active version supplied by the pinned startup.",
    )
    args = parser.parse_args()
    _STARTUP_STAGE = "component-source-check"
    if (
        args.component_source_sha256
        and hashlib.sha256(
            (
                CLIENT_ROOT
                / "src/agent_brain_client/adapters/platforms/macos/native/DeviceComponent.swift"
            ).read_bytes()
        ).hexdigest()
        != args.component_source_sha256
    ):
        raise ValueError("Native source review required")
    if args.mapping_refresh and (
        args.installation_check or args.event_hook or args.import_plan
    ):
        raise ValueError("Distinct mapping mode required")
    _STARTUP_STAGE = "content-policy-validation"
    policy = pinned(args.content_policy, args.content_policy_sha256)
    if not args.installation_check:
        _STARTUP_STAGE = "retained-memory-verification"
        require_memory_verified(args.device_home, args.https_profile_sha256)
    hook_policy = None
    hook_payload = None
    if args.event_hook:
        from agent_brain_client.adapters.harnesses.codex.hooks import (
            validate_hook_policy,
            hook_enabled,
            native_hook_fields,
            run_event_hook,
        )
        _STARTUP_STAGE = "event-hook-policy-validation"
        if args.installation_check or args.import_plan or not args.https_profile:
            raise ValueError("Distinct HTTPS native hook invocation required")
        hook_policy = validate_hook_policy(
            pinned(args.event_hook_policy, args.event_hook_policy_sha256),
            profile_sha256=args.https_profile_sha256,
            content_policy_sha256=args.content_policy_sha256,
        )
        if not hook_enabled(hook_policy):
            print("{}")
            return
        _STARTUP_STAGE = "event-hook-input-read"
        raw = sys.stdin.buffer.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("Hook input exceeds bound")
        _STARTUP_STAGE = "event-hook-payload-validation"
        hook_payload = json.loads(raw, object_pairs_hook=pairs)
        native_hook_fields(hook_policy, hook_payload)
    elif args.event_hook_policy or args.event_hook_policy_sha256:
        raise ValueError("Explicit hook mode required")
    if args.https_profile:
        _STARTUP_STAGE = "https-profile-load"
        if any(
            (
                args.device_plan,
                args.device_plan_sha256,
                args.signing_policy,
                args.signing_policy_sha256,
                args.component_source_sha256,
                args.ssh_profile,
                args.ssh_profile_sha256,
            )
        ):
            raise ValueError("Distinct transport policies required")
        from agent_brain_client.adapters.server.https import HTTPSContentClient

        client = HTTPSContentClient(
            args.https_profile, args.https_profile_sha256, credential_access=False
        )
        _STARTUP_STAGE = "credential-source-validation"
        source = CLIENT_ROOT / (
            "src/agent_brain_client/adapters/platforms/windows/native.py"
            if client.profile["platform"].startswith("win32-")
            else "src/agent_brain_client/adapters/platforms/macos/native/CredentialStore.swift"
        )
        if (
            not args.credential_source_sha256
            or hashlib.sha256(source.read_bytes()).hexdigest()
            != args.credential_source_sha256
            or (
                client.profile.get("credential_source_sha256")
                or client.profile["credential_backend"]["source_sha256"]
            )
            != args.credential_source_sha256
        ):
            raise ValueError("Credential helper source review required")
        _STARTUP_STAGE = "device-binding-validation"
        if (
            Path(client.profile["state_directory"])
            != Path(args.device_home) / ".config/agent-brain-client/https"
        ):
            raise ValueError("Credential device binding changed")
        if (
            "credential_helper" in client.profile
            and Path(args.device_home)
            not in Path(client.profile["credential_helper"]).parents
        ):
            raise ValueError("Device-local helper required")
        _STARTUP_STAGE = "identity-activation"
        client.activate_identity()
    elif args.https_profile_sha256 or args.credential_source_sha256:
        raise ValueError("HTTPS profile required")
    elif args.ssh_profile:
        _STARTUP_STAGE = "ssh-profile-load"
        if any(
            (
                args.device_plan,
                args.device_plan_sha256,
                args.signing_policy,
                args.signing_policy_sha256,
                args.component_source_sha256,
            )
        ):
            raise ValueError("Distinct transport policies required")
        from agent_brain_client.adapters.server.ssh import SSHContentClient

        client = SSHContentClient(args.ssh_profile, args.ssh_profile_sha256)
    else:
        if args.ssh_profile_sha256:
            raise ValueError("SSH profile required")
        _STARTUP_STAGE = "signing-policy-validation"
        signing = pinned(args.signing_policy, args.signing_policy_sha256)
        _STARTUP_STAGE = "device-component-load"
        client = ComponentClient(args.device_plan, args.device_plan_sha256, signing)
    _STARTUP_STAGE = "transport-policy-validation"
    if (
        bool(args.import_plan) != bool(args.import_plan_sha256)
        or args.installation_check
        and args.import_plan
    ):
        raise ValueError("Separate exact import approval required")
    if args.installation_check:
        _STARTUP_STAGE = "installation-check"
        print(json.dumps(installation_check(client, policy, args.client_version)))
        return
    _STARTUP_STAGE = "content-adapter-start"
    from agent_brain_client.runtime.harness_content import content_host
    native = args.codex_home if args.harness == "codex" else str(Path(args.device_home) / ".codebuddy")
    cache = args.cache if args.harness == "codex" else str(Path(args.device_home) / ".config/agent-brain-client/codebuddy/content-cache")
    adapter = ContentSession(
        args.device_home, cache, policy, client, host=content_host(args.harness), native_home=native
    )
    if args.mapping_refresh:
        result = adapter.refresh()
        print(json.dumps(result))
        if result.get("status") not in ("content-current", "content-refreshed"):
            raise SystemExit(2)
        return
    if args.event_hook:
        _STARTUP_STAGE = "event-hook-run"
        run_event_hook(adapter, hook_policy, hook_payload)
        # Hook output is protocol only; never inject captured text or continue a
        # completed turn. Receipt and pending evidence stay in the normal outbox.
        print("{}")
        return
    if args.import_plan:
        _STARTUP_STAGE = "import-plan-validation"
        path = Path(args.import_plan)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1048576:
            raise ValueError("Private bounded import plan required")
        private_mode(path)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != args.import_plan_sha256:
            raise ValueError("Import plan drift")
        adapter.sources.stage_import(json.loads(raw, object_pairs_hook=pairs))
    _STARTUP_STAGE = "mcp-serve"
    from agent_brain_client.runtime.delivery import supervise

    delivery_stop = supervise(args.device_home)
    try:
        serve(ContentServer(adapter, args.client_version), sys.stdin.buffer, sys.stdout)
    finally:
        delivery_stop.set()


def cli():
    try:
        # Windows cannot inherit a POSIX descriptor lease. The real worker also
        # holds its own shared LockFileEx lease, including if its launcher dies.
        if (
            os.name == "nt"
            and "--cache" in sys.argv
            and "--installation-check" not in sys.argv
        ):
            cache = Path(sys.argv[sys.argv.index("--cache") + 1])
            cache_directory(cache)
            with cache_locked(cache / ".content-runtime.lock", timeout=0, shared=True):
                from agent_brain_client.adapters.installation.installation import (
                    inspect,
                )

                home = Path(sys.argv[sys.argv.index("--device-home") + 1])
                if not inspect(home).get("installed"):
                    raise ValueError("Native installation changed before worker lease")
                main()
        else:
            main()
    except FreshMemoryVerificationRequired as error:
        print(json.dumps(error.result()), file=sys.stderr)
        if "--event-hook" in sys.argv:
            print("{}")
            return 0
        return 2
    except PlatformUnsupported as error:
        if "--event-hook" in sys.argv:
            print("{}")
            emit_startup_failure(_STARTUP_STAGE, error)
            return 0
        print(
            json.dumps(error.result(served=False, native_changed=False)),
            file=sys.stderr,
        )
        return 2
    except Exception as error:
        if "--event-hook" in sys.argv:
            # Some failures happen before staging exists. Report only the
            # bounded stage/category; never claim that local evidence was saved.
            print("{}")
            emit_startup_failure(_STARTUP_STAGE, error)
            return 0
        return emit_startup_failure(_STARTUP_STAGE, error)
    return 0


if __name__ == "__main__":
    sys.exit(cli())
