"""Authenticated collection protocol over the enrolled HTTPS transport."""

from functools import cached_property
import json
import math
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request
from agent_brain_client.adapters.storage import metadata as metadata
from agent_brain_client.adapters.storage import registration as registration_store
from agent_brain_client.domain.errors import GlobalRequestTooLarge
from agent_brain_client.domain.events import (
    MAX_NATIVE_MEMORY_BYTES, NATIVE_MEMORY_CONTRACT, PROJECT_ORIGIN_CAPABILITY,
)
from agent_brain_client.adapters.storage.workspace_identity import workspace_project
from agent_brain_client.domain.scopes import (
    digest,
    select,
    source_id,
    namespace,
)
from agent_brain_client.adapters.storage.collection import approved

MAX_GLOBAL_REQUEST_BYTES = 128 * 1024
# A control byte can expand to six JSON bytes. Preserve the old envelope for
# everything but the explicitly scoped native original's encoded body.
MAX_NATIVE_REQUEST_BYTES = 6 * MAX_NATIVE_MEMORY_BYTES + MAX_GLOBAL_REQUEST_BYTES


class CollectionClient:
    """Dedicated, validated global protocol over the existing HTTPS identity."""

    def __init__(self, value, local=None):
        self.value, self.local = value, local
        from agent_brain_client.adapters.server.https import HTTPSContentClient

        _, registered = registration_store.registration(value["device_home"])
        r = registered["runtime"]
        from agent_brain_client.adapters.storage.reinstallation import (
            require_collection_ready,
        )

        require_collection_ready(value["device_home"], r["https_profile_sha256"])
        self.base = HTTPSContentClient(r["https_profile"], r["https_profile_sha256"])
        self.grant, self.data_mode = self.base.grant, self.base.data_mode
        self.actor, self.device = self.grant["actor"], self.base.device_id
        self.device_id = self.device
        self.profile = self.base.profile
        self.profile_sha = self.base.profile_sha
        self.scope_sha = digest(value["scope"])
        self.queue_identity = dict(
            self.base.queue_identity,
            global_profile=value["scope"]["profile_id"],
            origin_sha=digest(local),
        )

    def wire(self, method, params, rid=None):
        allowed = {
            "memory_global_status",
            "memory_query",
            "memory_global_query",
            "memory_global_event_submit",
            "memory_global_stop",
            "memory_global_resume",
            "memory_global_sync",
            "memory_global_register",
        }
        if method not in allowed:
            raise ValueError("Global memory operation required")
        rid = rid or "global_" + os.urandom(16).hex()
        raw = self._wire_body(method, params, rid)
        from agent_brain_client.application.control import (
            check_request,
            recall_timing_stage,
            request_timeout,
        )

        check_request()
        token = (
            self.base._credential()
        )  # Existing configured secure reference; never returned.
        check_request()
        req = Request(
            self.base.profile["endpoint"].rstrip("/") + "/v1/requests",
            data=raw,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "X-Agent-Brain-Adapter": "codex",
                "X-Agent-Brain-Protocol": "1",
                "X-Agent-Brain-State-Schema": "1",
                "X-Agent-Brain-Data-Mode": "personal",
                # The bounded body check already restricted this opt-in to
                # the native-memory event. The receiver authenticates first.
                **({"X-Agent-Brain-Native-Memory": "1"} if len(raw) > MAX_GLOBAL_REQUEST_BYTES else {}),
            },
        )

        def decode(body):
            if len(body) > 262144:
                raise ValueError("Bounded global response required")
            return json.loads(
                body,
                object_pairs_hook=metadata.unique,
                parse_float=lambda v: float(v)
                if math.isfinite(float(v))
                else (_ for _ in ()).throw(ValueError("Non-finite response")),
                parse_constant=lambda _: (_ for _ in ()).throw(
                    ValueError("Non-finite response")
                ),
            )

        try:
            with self.base.transport.opener.open(req, timeout=request_timeout(90)) as response:
                with recall_timing_stage("response_body"):
                    body = response.read(262145)
                result = decode(body)
        except HTTPError as error:
            try:
                data = decode(error.read(262145))
                code = data.get("error", {}).get("code")
                if not isinstance(code, str) or not re.fullmatch(
                    r"[a-z][a-z0-9_]{0,63}", code
                ):
                    code = "global_http_unconfirmed"
            except Exception:
                code = "global_http_unconfirmed"
            return {
                "request_id": rid,
                "error": {"code": code, "retryable": error.code >= 500},
                "persistence": {"durable": False},
            }
        except (URLError, OSError):
            return {
                "request_id": rid,
                "error": {"code": "transport_unconfirmed", "retryable": True},
                "persistence": {"durable": False},
            }
        if (
            not isinstance(result, dict)
            or result.get("request_id") != rid
            or not isinstance(result.get("result"), dict)
            and "error" not in result
        ):
            raise ValueError("Global response identity changed")
        return result

    @cached_property
    def capabilities(self):
        result = self.wire("memory_global_status", {})
        names = result.get("capabilities")
        if "error" in result or not isinstance(names, list):
            return frozenset()
        return frozenset(
            name for name in names if name in (PROJECT_ORIGIN_CAPABILITY, "final-context-v1")
        )

    def recall_workspace_project(self):
        return workspace_project(self.local)

    def params(self, session, *, recall=False):
        if (
            self.local is None
            or select(self.value["scope"], self.local, recall=recall) is None
            or not approved(self.value)
        ):
            raise ValueError("Origin no longer approved")
        return {"origin": self.local, "session_id": session}

    def status(self, session, *, event_id=None, event_only=False):
        params = self.params(session)
        if event_only and event_id is None:
            raise ValueError("Exact event status requires an event ID")
        if event_id is not None:
            from agent_brain_client.domain.events import event_identifier

            event_identifier(session)
            event_identifier(event_id)
            params["event_id"] = event_id
        if event_only:
            params["event_only"] = True
        result = self.wire("memory_global_status", params)
        if "error" in result:
            raise ValueError("Global admission unavailable")
        state = result["result"]
        if event_id is not None and not isinstance(
            state.get("capture_extraction"), dict
        ):
            from agent_brain_client.domain.errors import EventDiagnosticsUnavailable

            raise EventDiagnosticsUnavailable(
                "Event diagnostics unavailable on this Server"
            )
        if event_only:
            return state
        if (
            state.get("global_profile_id") != self.value["scope"]["profile_id"]
            or state.get("scope_sha256") != self.scope_sha
        ):
            raise ValueError("Global source approval changed")
        return state

    @staticmethod
    def _wire_body(method, params, rid):
        request = {"request_id": rid, "method": method, "params": params}
        raw = json.dumps(request, ensure_ascii=False).encode("utf-8")
        if len(raw) <= MAX_GLOBAL_REQUEST_BYTES:
            return raw
        event = params.get("event") if isinstance(params, dict) else None
        if (
            len(raw) <= MAX_NATIVE_REQUEST_BYTES
            and method == "memory_global_event_submit"
            and isinstance(event, dict)
            and event.get("contract") == NATIVE_MEMORY_CONTRACT
            and isinstance(event.get("content"), str)
            and len(event["content"].encode("utf-8")) <= MAX_NATIVE_MEMORY_BYTES
        ):
            envelope = dict(request, params=dict(params, event=dict(event, content="")))
            if len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= MAX_GLOBAL_REQUEST_BYTES:
                return raw
        raise GlobalRequestTooLarge("Bounded global request required")

    def check_event_frame(self, request):
        """Check the actual encoding before an unsendable original enters the outbox."""
        self._wire_body(
            "memory_global_event_submit", self._event_params(request), request["request_id"],
        )

    def _event_params(self, request, *, project=None):
        from agent_brain_client.domain.contract import validate as contract_validate

        params = request["params"]
        contract_validate("memory_event_submit", params)
        event = params["event"]
        selected = select(self.value["scope"], self.local)
        if selected is None:
            raise ValueError("Global event outside source policy")
        sid = source_id(self.value["scope"], self.local, selected["sharing"])
        native = event["contract"] == NATIVE_MEMORY_CONTRACT
        if event["project_id"] != namespace(self.actor, sid):
            raise ValueError("Global event outside source policy")
        if native:
            wire_event = {
                key: event[key] for key in ("contract", "event_id", "occurred_at", "content")
            }
        else:
            wire_event = event
        bound = self.params(event["session_id"])
        if project is not None:
            if self.local["kind"] != "project":
                raise ValueError("Non-project event cannot claim a workspace project")
            bound["origin"] = dict(self.local, project=project)
        return dict(bound, event=wire_event)

    def request(self, request):
        if request.get("method") != "memory_event_submit":
            return {
                "request_id": request.get("request_id"),
                "error": {"code": "global_method_denied", "retryable": False},
                "persistence": {"durable": False},
            }
        from agent_brain_client.domain.contract import core_receipt

        source = getattr(self, "event_source_project", None)
        project = None if source is None else source(
            request, lambda: PROJECT_ORIGIN_CAPABILITY in self.capabilities,
        )
        result = self.wire(
            "memory_global_event_submit",
            self._event_params(request, project=project),
            request["request_id"],
        )
        if "error" not in result and not core_receipt(result, request):
            raise ValueError("Unconfirmed global Core receipt")
        return result
