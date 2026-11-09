"""Authenticated collection protocol over the enrolled HTTPS transport."""

import json
import math
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request
from agent_brain_client.adapters.storage import metadata as metadata
from agent_brain_client.adapters.storage import registration as registration_store
from agent_brain_client.domain import privacy as privacy
from agent_brain_client.domain.scopes import (
    digest,
    select,
    source_id,
    namespace,
    restricted,
)
from agent_brain_client.adapters.storage.collection import approved


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
        request = {"request_id": rid, "method": method, "params": params}
        raw = json.dumps(request, ensure_ascii=False).encode()
        if len(raw) > 131072:
            raise ValueError("Bounded global request required")
        from agent_brain_client.application.control import check_request, request_timeout

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
                result = decode(response.read(262145))
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

    def params(self, session):
        if (
            self.local is None
            or select(self.value["scope"], self.local) is None
            or not approved(self.value)
        ):
            raise ValueError("Origin no longer approved")
        return {"origin": self.local, "session_id": session}

    def status(self, session, *, event_id=None):
        params = self.params(session)
        if event_id is not None:
            from agent_brain_client.domain.events import event_identifier

            event_identifier(session)
            event_identifier(event_id)
            params["event_id"] = event_id
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
        if (
            state.get("global_profile_id") != self.value["scope"]["profile_id"]
            or state.get("scope_sha256") != self.scope_sha
        ):
            raise ValueError("Global source approval changed")
        return state

    def request(self, request):
        if request.get("method") != "memory_event_submit":
            return {
                "request_id": request.get("request_id"),
                "error": {"code": "global_method_denied", "retryable": False},
                "persistence": {"durable": False},
            }
        from agent_brain_client.domain.contract import (
            validate as contract_validate,
            core_receipt,
        )

        params = request["params"]
        contract_validate("memory_event_submit", params)
        event = params["event"]
        selected = select(self.value["scope"], self.local)
        sid = source_id(self.value["scope"], self.local, selected["sharing"])
        if event["project_id"] != namespace(self.actor, sid) or any(
            (privacy.private_content(m["content"]) or restricted(m["content"]))
            for m in event["messages"]
        ):
            raise ValueError("Global event outside source policy")
        result = self.wire(
            "memory_global_event_submit",
            dict(self.params(event["session_id"]), event=event),
            request["request_id"],
        )
        if "error" not in result and not core_receipt(result, request):
            raise ValueError("Unconfirmed global Core receipt")
        return result
