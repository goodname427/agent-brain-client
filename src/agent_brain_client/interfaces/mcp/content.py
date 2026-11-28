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
    write_response,
    ContentParameterError,
    ContentToolUnavailable,
)


from agent_brain_client.domain.tool_contract import TOOL_CONTRACT_VERSION
from agent_brain_client.domain.operations import CONTENT_WRITES, LIFECYCLE_WRITES
from agent_brain_client.application.queue.outbox import QUEUE_DIAGNOSTIC, content_write_request
from agent_brain_client.application.tools.catalog import (
    RECALL_EVIDENCE_GUIDANCE,
    tool_definitions,
    LIFECYCLE_ACTION_METHODS,
    LIFECYCLE_METHODS,
    LIFECYCLE_TOOL,
    authorized_content_methods,
    memory_sources_authorized,
    tool_names,
    enabled_tools,
)
from agent_brain_client.adapters.storage.pinned import pinned

from agent_brain_client.interfaces.mcp.server import (
    DRAIN_METHOD, DRAIN_PREFIX, HANDOFF_VERSION, runtime_files, serve,
)
from agent_brain_client.domain.errors import CacheBlocked
from agent_brain_client.adapters.storage.reinstallation import require_memory_verified
from agent_brain_client.application.installation.fresh import (
    FreshMemoryVerificationRequired,
)

from agent_brain_client.adapters.storage.origins import discover as discover_origin


from agent_brain_client.application.memory.guidance import MEMORY_GUIDANCE
from agent_brain_client.application.memory.onboarding import TOOL_NAME

import argparse
import hashlib
import json
import os
import re
import ssl
import threading
from urllib.error import HTTPError
import uuid
from agent_brain_client.interfaces.mcp.topics import Server, VERSIONS
from agent_brain_client.adapters.storage.content_session import (
    ContentSession,
    contract,
    ROOT as CLIENT_ROOT,
)
from agent_brain_client.adapters.storage.content import CONTENT_CACHE_ABI, operation_diagnostics
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


MCP_WORKER_HANDOFF = 1


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
    update_guidance = UPDATE_GUIDANCE
    managed_stdio = False

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
            "running_delivery_version": None,
            "running_delivery_commit": None,
            "delivery_worker_status": "unknown",
            "reload_required": None,
            # This worker cannot see the host's session tool cache. A current
            # worker is not proof that the host has adopted its tool list.
            "reload_required_scope": "mcp-worker",
            "reload_reason": "active-package-unobserved",
        }
        if not isinstance(getattr(self.adapter, "home", None), Path):
            return value
        try:
            from agent_brain_client.adapters.storage.private_files import (
                private_json,
            )
            from agent_brain_client.adapters.packages.client import ClientReleases
            from agent_brain_client.runtime.delivery import runtime_status

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
            value.update(runtime_status(self.adapter.home, active))
            if self.managed_stdio:
                current = json.loads((CLIENT_ROOT / "verified.json").read_bytes())["manifest"]
                selected = json.loads(
                    (Path(active["package"]) / "verified.json").read_bytes()
                )["manifest"]
                if runtime_files(current) == runtime_files(selected):
                    value.update(reload_required=False, reload_reason="running-worker-content-current")
                    return value
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
        method = name
        if name == "memory_query":
            return self.layered_recall(arguments)
        try:
            if (
                name.startswith("content_sources_")
                and not self._memory_source_tools_authorized()
            ):
                raise ContentParameterError(
                    "Native Memory source tools are outside the granted Memory scope."
                )
            if name == TOOL_NAME:
                if name not in tool_names(
                    self.adapter.methods(), self._memory_source_tools_authorized()
                ):
                    raise ContentToolUnavailable("Onboarding requires an existing Memory write grant.")
                from agent_brain_client.runtime.onboarding import onboard_native_memory

                result = onboard_native_memory(self.adapter, arguments)
                return {
                    "isError": "error" in result,
                    "structuredContent": result,
                    "content": [
                        {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                    ],
                }
            methods = self.adapter.methods()
            if name == LIFECYCLE_TOOL:
                if not LIFECYCLE_METHODS.intersection(methods):
                    raise ContentToolUnavailable("Content lifecycle is outside the installed tool grant.")
            elif (
                name == "content_set_enabled" or name in contract.HOOK_METHODS
                or name in LIFECYCLE_METHODS or name not in methods
            ):
                raise ContentToolUnavailable(
                    "Tool is not exposed by this worker. Inspect tools/list and the installed tool grant; retry the normal MCP connection if it failed to initialize. Tool absence alone does not prove a remote outage."
                )
            if not isinstance(arguments, dict):
                raise ContentParameterError("Content tool arguments must be an object.")
            params = dict(arguments)
            if name == LIFECYCLE_TOOL:
                action = params.pop("action", None)
                if not isinstance(action, str) or action not in LIFECYCLE_ACTION_METHODS:
                    raise ContentParameterError("Use an action listed in the content_lifecycle input schema.")
                method = LIFECYCLE_ACTION_METHODS[action]
                if method not in methods:
                    raise ContentToolUnavailable("This lifecycle action is outside the installed tool grant.")
            elif name == "content_put" and "operation" in params:
                if params.pop("operation") != "set_enabled":
                    raise ValueError()
                method = "content_set_enabled"
                if method not in methods:
                    raise ContentToolUnavailable("Rule/Skill activation is outside the installed tool grant.")
            required = contract.FIELDS[method]
            allowed = (
                contract.FIELDS[method]
                | ({"cursor"} if name == "content_catalog" else set())
                | ({"include_archived"} if name == "content_read" and self._lifecycle_enabled() else set())
                | {"request_id"}
                | (
                    {"reconciles", "source_context"}
                    if method == "content_put"
                    else {"reconciles"}
                    if method == "content_set_enabled"
                    else set()
                )
            )
            if set(params) - allowed or required - set(params):
                raise ValueError()
            reconciles = params.pop("reconciles", None)
            if "request_id" in params and (
                not isinstance(params["request_id"], str)
                or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", params["request_id"]) is None
            ):
                raise ValueError()
            rid = params.pop("request_id", None)
            contract.validate(method, params)
        except ContentParameterError:
            raise
        except CacheBlocked:
            raise ProtocolError(
                -32010,
                "Onboarding or local source state is unavailable. Restore the approved Client binding before retrying.",
            ) from None
        except (ValueError, TypeError):
            message = (
                "Invalid "
                + name
                + " arguments; use the exact fields and types in this tool input schema."
            )
            if method in LIFECYCLE_WRITES:
                message += " Use the current revision from content_read with include_archived:true."
            elif method in contract.WRITES:
                message += " Writes require the expected_revision obtained from content_read."
            raise ContentParameterError(message) from None
        write = method in CONTENT_WRITES
        request = (
            content_write_request(method, params, request_id=rid)
            if write
            else {"request_id": rid or uuid.uuid4().hex, "method": method, "params": params}
        )
        call_options = {"reconciles": reconciles} if reconciles is not None else {}
        with operation_diagnostics() as diagnostic:
            try:
                if method == "content_put" and params["kind"] == "memory" and "source_context" not in params:
                    session = os.environ.get("CODEX_THREAD_ID")
                    if not isinstance(session, str) or not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", session
                    ):
                        session = None
                    try:
                        origin = discover_origin()
                    except OSError:
                        origin = None
                    call_options["memory_context"] = {"origin": origin, "session_id": session}
                result = self.adapter.invoke(request, **call_options)
            except RequestStopped:
                raise
            except Exception as error:
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
                    "request_id": request["request_id"],
                    "error": {
                        "code": "content_busy" if busy else "content_adapter_blocked",
                        "retryable": busy,
                        "stage": diagnostic["stage"],
                        "exception_type": exception_type,
                    },
                    "persistence": {"durable": False},
                }
        if write and "error" in result:
            queue = result.get("local_queue", {})
            error = result["error"]
            state = queue.get("state", "unconfirmed")
            code = error.get("code")
            if code not in (
                "content_upload_queued", "revision_conflict", "permission_denied",
                "invalid_request", "content_busy", "content_adapter_blocked",
                "device_method_denied", "device_authentication_failed", "content_activation_denied",
            ):
                code = "content_write_blocked"
            result = {
                "request_id": request["request_id"],
                # A pending write is retried by the Client. A new tools/call
                # without a legacy ID would create an independent write.
                "error": {
                    "code": code,
                    "retryable": state != "pending" and error.get("retryable") is True,
                },
                "persistence": {"durable": False},
                "local_queue": {
                    "state": state,
                    "server_saved": False,
                    "next_action": queue.get("next_action") or (
                        f"Check the write using {QUEUE_DIAGNOSTIC} before resubmitting."
                    ),
                },
            }
        else:
            result = dict(result, client_runtime=self.client_runtime)
        return {
            "isError": "error" in result,
            "structuredContent": result,
            "content": [
                {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
            ],
        }

    def _lifecycle_enabled(self):
        return getattr(getattr(self.adapter, "client", None), "supports_content_lifecycle", True)

    def catalog(self):
        return tool_definitions(
            self.adapter.methods(),
            getattr(self.adapter, "policy", {}).get("kinds", ["memory", "rule", "skill"]),
            self._memory_source_tools_authorized(),
            self.client_version,
            self.update_guidance,
            lifecycle=self._lifecycle_enabled(),
        )

    def initial_mapping(self):
        return self.adapter.refresh()

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
            return {"tools": self.catalog()}
        result = super().handle(request)
        if (
            isinstance(request, dict)
            and request.get("method") == "initialize"
            and isinstance(result, dict)
        ):
            mapping = self.initial_mapping()
            result["content_mapping"] = mapping
            result["serverInfo"]["name"] = "agent-brain-content"
            result["instructions"] = (
                RECALL_EVIDENCE_GUIDANCE
                + MEMORY_GUIDANCE
                + "content_catalog lists authorized AgentBrain destination records only; it does not discover Harness-native sources. "
                + "Ordinary CAS writes require the current expected_revision. The Client retains and retries unconfirmed writes automatically. "
                + "Pending/local-confirmed/remote-confirmed and durable fields distinguish client, Server and backup status. "
                + "Ordinary edits retain Rule/Skill enabled state. Use content_put with operation=set_enabled only for an explicit user request to enable or disable a Rule/Skill. Refreshing mappings does not reload an already running model. "
                + "Only granted methods and IDs are exposed; pairing and publication remain separate. "
                + "Native mapping result: "
                + json.dumps(mapping, sort_keys=True)
                + ". "
                + self.update_guidance
            )
        return result


class ManagedContentServer(ContentServer):
    """Resume a previously negotiated stdio session without native refresh."""

    managed_stdio = True
    update_guidance = UPDATE_GUIDANCE.replace(
        "Matching identities need no worker reload; differing identities need a normal reload of only Agent Brain MCP after in-flight work.",
        "Compatible tool contracts can switch workers on this connection after in-flight work. "
        "Confirm client_runtime after the update. When the verified registration, cache ABI or tool contract changes, use the normal Agent Brain MCP reload after in-flight work.",
    )

    def __init__(self, adapter, client_version, protocol):
        super().__init__(adapter, client_version)
        if protocol != "initial":
            self.state = "ready"

    def initial_mapping(self):
        return {"status": "content-refresh-scheduled", "native_changed": None, "model_loaded": "unknown"}

    def handle(self, request):
        if (
            isinstance(request, dict)
            and request.get("method") == DRAIN_METHOD
            and isinstance(request.get("id"), str)
            and request["id"].startswith(DRAIN_PREFIX)
        ):
            return {}
        return super().handle(request)

    def readiness(self):
        return {
            "abWorkerReady": HANDOFF_VERSION,
            "versions": list(VERSIONS),
            "capabilities": {"tools": {}},
            "toolContract": TOOL_CONTRACT_VERSION,
            "cacheIdentity": self.adapter.queue_sha,
            "cacheAbi": CONTENT_CACHE_ABI,
            "tools": self.catalog(),
        }


class DeliveryInput:
    def __init__(self, source, start):
        self.source = source
        self.start = start

    def readline(self, limit):
        line = self.source.readline(limit)
        if line:
            self.start.set()
        return line


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


def installation_check(client, policy, client_version="0.2.0"):
    """Fixed read-only MCP/auth check. No content cache/native mapping is created."""
    methods = authorized_content_methods(
        policy, client.grant, lifecycle=getattr(client, "supports_content_lifecycle", True)
    )
    expected_tools = set(tool_names(methods, memory_sources_authorized(policy, client.grant)))

    class Adapter:
        def __init__(self):
            self.policy = policy
            self.client = client

        def refresh(self):
            return {"status": "protocol-only", "native_changed": False}

        def methods(self):
            return methods

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
    listed_names = [tool["name"] for tool in tools["tools"]]
    if (
        first["serverInfo"]["name"] != "agent-brain-content"
        or len(listed_names) != len(set(listed_names))
        or set(listed_names) != expected_tools
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
                    "tools": enabled_tools(
                        value["policy"], value["grant"], lifecycle=value.get("lifecycle", True)
                    ),
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
    parser.add_argument("--managed-stdio", choices=("initial", *VERSIONS), help=argparse.SUPPRESS)
    parser.add_argument("--own-runtime-lease", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--background-content", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--background-registration", help=argparse.SUPPRESS)
    parser.add_argument("--background-commit", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.managed_stdio and (
        not args.https_profile or args.installation_check or args.event_hook
        or args.mapping_refresh or args.import_plan or args.background_content
    ):
        raise ValueError("Managed stdio requires ordinary HTTPS MCP serving")
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
        args.installation_check or args.event_hook or args.import_plan or args.background_content
    ):
        raise ValueError("Distinct mapping mode required")
    if args.background_content:
        if (
            not args.https_profile or args.installation_check or args.event_hook or args.import_plan
            or not args.background_registration or not args.background_commit
            or worker_commit(args.client_version) != args.background_commit
        ):
            raise ValueError("Verified background content runtime required")
        from agent_brain_client.runtime.launcher import registration_stamp
        from agent_brain_client.interfaces.mcp.launcher import runtime_fingerprint

        def still_current():
            registration = registration_stamp(
                {"device_home": args.device_home, "codex_home": args.codex_home}, args.harness
            )
            return registration is not None and runtime_fingerprint(registration) == args.background_registration

        if not still_current():
            raise ValueError("Background content binding changed")
    elif args.background_registration or args.background_commit:
        raise ValueError("Distinct background content mode required")
    _STARTUP_STAGE = "content-policy-validation"
    policy = pinned(args.content_policy, args.content_policy_sha256)
    if not args.installation_check:
        _STARTUP_STAGE = "retained-memory-verification"
        require_memory_verified(args.device_home, args.https_profile_sha256)
    hook_policy = None
    hook_payload = None
    if args.event_hook:
        from agent_brain_client.runtime.harness_content import event_hook_host
        hooks = event_hook_host(args.harness)
        _STARTUP_STAGE = "event-hook-policy-validation"
        if args.installation_check or args.import_plan or not args.https_profile:
            raise ValueError("Distinct HTTPS native hook invocation required")
        hook_policy = hooks.validate_hook_policy(
            pinned(args.event_hook_policy, args.event_hook_policy_sha256),
            profile_sha256=args.https_profile_sha256,
            content_policy_sha256=args.content_policy_sha256,
        )
        if not hooks.hook_enabled(hook_policy):
            print("{}")
            return
        _STARTUP_STAGE = "event-hook-input-read"
        raw = sys.stdin.buffer.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("Hook input exceeds bound")
        _STARTUP_STAGE = "event-hook-payload-validation"
        hook_payload = json.loads(raw, object_pairs_hook=pairs)
        hooks.native_hook_fields(hook_policy, hook_payload)
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
    host = content_host(args.harness)
    native, cache = host.location(Path(args.device_home), {"codex_home": args.codex_home, "content_cache": args.cache})
    adapter = ContentSession(
        args.device_home, cache, policy, client, host=host, native_home=native,
        existing_cache=args.managed_stdio not in (None, "initial"),
    )
    if args.background_content:
        from agent_brain_client.runtime.background import content_refresh

        print(json.dumps(content_refresh(
            adapter, args.background_commit, args.background_registration, still_current=still_current,
        )))
        return
    if args.mapping_refresh:
        result = adapter.refresh()
        print(json.dumps(result))
        if result.get("status") not in ("content-current", "content-refreshed"):
            raise SystemExit(2)
        return
    if args.event_hook:
        _STARTUP_STAGE = "event-hook-run"
        hooks.run_event_hook(adapter, hook_policy, hook_payload)
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

    if args.managed_stdio:
        server = ManagedContentServer(adapter, args.client_version, args.managed_stdio)
        delivery_start = threading.Event()
        source = DeliveryInput(sys.stdin.buffer, delivery_start)
    else:
        server = ContentServer(adapter, args.client_version)
        delivery_start = None
        source = sys.stdin.buffer
    delivery_stop = supervise(args.device_home, start=delivery_start)
    try:
        if args.managed_stdio:
            write_response(sys.stdout, server.readiness())
        serve(server, source, sys.stdout, managed_stdio=bool(args.managed_stdio))
    finally:
        delivery_stop.set()


def cli():
    try:
        # Isolated supervisors launch the exact worker directly. The worker
        # owns its cache lease for precisely its lifetime, including at abort.
        # The preexisting Windows fallback also uses a worker-owned lease.
        own_lease = "--own-runtime-lease" in sys.argv
        if own_lease and ("--cache" not in sys.argv or not (
            "--managed-stdio" in sys.argv or "--background-content" in sys.argv
        )):
            raise ValueError("Managed content cache lease required")
        if (
            (os.name == "nt" or own_lease)
            and "--cache" in sys.argv
            and "--installation-check" not in sys.argv
        ):
            cache = Path(sys.argv[sys.argv.index("--cache") + 1])
            cache_directory(cache)
            lock = cache / ".content-runtime.lock"
            if own_lease:
                from agent_brain_client.adapters.platforms.filesystem import cache_mode, file_mode

                cache_mode(cache, directory=True, owner=True)
                try:
                    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    try:
                        if os.name != "nt":
                            file_mode(fd, 0o600)
                    finally:
                        os.close(fd)
                if lock.is_symlink():
                    raise ValueError("Content lease changed")
                cache_mode(lock, owner=True)
            with cache_locked(lock, timeout=0, shared=True):
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
