"""Run Client Onboarding against isolated Server routing and the real SQLite Core.

Provide a reviewed Server checkout and its offline Core fixture environment
(AGENTBRAIN_TEST_CORE, TEST_CORE_SOURCE, TEST_NODE). Only fictional auth, sources,
and temporary state are used. The HTTP opener dispatches in process.
"""

import argparse
from contextlib import contextmanager
import io
import json
from pathlib import Path
import sys
from threading import RLock
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError


def verify(server_root, harness="codex"):
    root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(root / "src"), str(root / "tests"),
                    str(server_root / "src"), str(server_root / "tests")]
    from test_global_pool import Pool
    from test_native_continuous_extraction import NativeContinuousExtraction
    from agent_brain_server.backends.tencent.adapter import TencentAdapter
    from agent_brain_server.contracts.v1.errors import Rejected
    from agent_brain_server.contracts.v1.native import Identity
    from agent_brain_server.support.recall_handoff import RecallHandoff
    from agent_brain_server.memory.sources.router import _native_session
    from agent_brain_server.persistence.source_registry import SourceRegistry
    from agent_brain_client.adapters.server.collection import CollectionClient
    from agent_brain_client.adapters.platforms.filesystem import native_platform
    from agent_brain_client.application.memory.onboarding import SOURCE_SCOPE
    from agent_brain_client.adapters.storage import metadata, registration
    from agent_brain_client.adapters.storage.content_session import ContentSession
    from agent_brain_client.domain.scopes import digest, namespace
    from agent_brain_client.interfaces.mcp.content import ContentServer
    from agent_brain_client.runtime import collection, delivery, onboarding
    from agent_brain_client.runtime.harness_content import content_host
    from test_content_outbox import Cloud, POLICY

    core, server = NativeContinuousExtraction(), Pool()
    try:
        core.setUp()
        server.setUp()

        @contextmanager
        def journal(*, read_only=False):
            # The fixture uses one in-memory database, including its read path.
            yield server.db

        server.service.source_records = SourceRegistry(journal)
        home = server.root / "client"
        home.mkdir()
        work = home / "fictional-work"
        (work / ".git").mkdir(parents=True)
        project_work = home / "fictional-project-route"
        (project_work / ".git").mkdir(parents=True)
        excluded_work = home / "fictional-excluded-origin"
        (excluded_work / ".git").mkdir(parents=True)
        native = home / ("." + harness)
        original = "Fictional preference for native SQLite boundary verification."
        uncertain = "Fictional offline native memory approved in the second batch."
        denied = "Fictional oversized original. " * 5000
        memory = native / ("projects/unknown/memory" if harness == "codebuddy" else "memories")
        memory.mkdir(parents=True)
        (memory / "approved.md").write_text(original)
        (memory / "local.md").write_text(denied)

        hub = server.mac
        hub.scope["include_roots"] = [
            {"root": str(work), "sharing": "personal"},
            {"root": str(project_work), "sharing": "project"},
        ]
        hub.scope["exclude_roots"] = [str(excluded_work)]
        hub.owner.update(team_id="fictional-team", user_id="fictional-user")
        hub.value = {"legacy_projects": {}}
        hub.dispatch, hub.recall_handoff = RLock(), RecallHandoff()
        hub.allowance_status = lambda: {"source_admitted": True, "model_admitted": False}
        hub.start = lambda: None  # Proof ends at L0, with no model or background worker.
        hub.directory = server.root / "router"
        sid = hub.scope["profile_id"]
        group = "native-source-" + digest(content_host(harness).binding(home, native))
        approved_origin = collection.discover(work)
        approved_identity = Identity(
            hub.owner["team_id"], hub.owner["user_id"], hub.owner["device_id"],
            namespace(hub.owner["actor"], sid), _native_session(approved_origin, group),
        )
        core.core.close()
        core.identities = [approved_identity]
        core.start()
        receipt_dir = hub.directory / "namespaces" / sid / "receipts"
        receipt_dir.mkdir(parents=True, mode=0o700)
        hub.index.write_text(json.dumps({sid: {}}))
        hub.index.chmod(0o600)
        adapter = TencentAdapter(str(receipt_dir / "adapter.sqlite3"), lambda: core.core)
        server.addCleanup(adapter.close)
        events, identities, errors = [], [], []
        lose_next = False

        def native_submit(request, _principal, _client, _write_receipt, *, native_source):
            assert native_source
            event = request["params"]["event"]
            identity = Identity(hub.owner["team_id"], hub.owner["user_id"], hub.owner["device_id"],
                                event["project_id"], event["session_id"])
            assert identity == approved_identity
            core.core.ensure_identity(identity)
            result = adapter.submit(identity, event)
            identities.append(identity)
            return {"request_id": request["request_id"], "result": result,
                    "persistence": {"state": "core-accepted", "durable": False,
                                    "github_confirmed": False, "receipt_ledger_durable": True}}

        hub._child = lambda _scope: SimpleNamespace(handle=native_submit)
        principal = dict(server.grant, device_id=hub.owner["device_id"])

        def open_in_process(request, **_):
            nonlocal lose_next
            wire = json.loads(request.data)
            submitted = wire["method"] == "memory_global_event_submit"
            try:
                result = server.pool.handle(wire, principal)
            except Exception as exc:
                errors.append((type(exc).__name__, str(exc)))
                raise
            if submitted:
                events.append(wire)
                if lose_next:
                    lose_next = False
                    raise URLError("Fictional connection loss after the committed response")
            return io.BytesIO(json.dumps(result).encode())

        cloud = Cloud()
        cloud.queue_identity = {"device_id": hub.owner["device_id"]}
        cloud.grant["platform"] = native_platform()
        endpoint = "https://synthetic.invalid"
        settings = {"device_home": str(home), "legacy_projects": [], "scope": hub.scope}
        policy = home / "policy.json"
        pin = metadata.write(policy, POLICY)
        enrolled = {"runtime": {"content_policy": str(policy), "content_policy_sha256": pin,
                                "codex_home": str(native), "content_cache": str(home / "content")}}

        def transport(value, origin):
            client = object.__new__(CollectionClient)
            client.value, client.local, client.actor = value, origin, principal["actor"]
            client.base = SimpleNamespace(
                queue_identity=cloud.queue_identity, _credential=lambda: "fictional-isolated-transport",
                grant=cloud.grant,
                profile={"endpoint": endpoint}, transport=SimpleNamespace(opener=SimpleNamespace(open=open_in_process)),
            )
            client.grant = cloud.grant
            client.queue_identity = dict(cloud.queue_identity, origin_sha=digest(origin))
            client.scope_sha = digest(hub.scope)
            return client

        def call(arguments):
            content = ContentSession(home, home / "content", POLICY, cloud,
                                     host=content_host(harness), native_home=native)
            worker = ContentServer(content)
            worker.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "fictional-onboarding", "version": "1"},
            }})
            worker.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
            result = worker.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "memory_onboard", "arguments": arguments,
            }})
            return result["structuredContent"]

        def run(arguments):
            result = call(arguments)
            if result["counts"]["pending"]:
                approval_id = result["approval_id"]
                tasks = [metadata.private_json(path, cache=True) for path in delivery.directory(home).glob("*.json")]
                task, = [task for task in tasks if task.get("approval_id") == approval_id]
                onboarding.deliver_approved_batch(home, task, collection.native_services())
                result = call(dict(args, resume=True, approval_id=approval_id))
            return result

        args = {"harness": harness, "source_scope": SOURCE_SCOPE,
                "destination": {"endpoint": endpoint, "actor": principal["actor"], "origin": str(work)}}

        with (
            patch.object(collection, "load", return_value=settings),
            patch.object(collection, "approved", return_value=True),
            patch("agent_brain_client.adapters.server.collection.approved", return_value=True),
            patch.object(collection, "CollectionClient", side_effect=transport),
            patch.object(registration, "registration", return_value=({}, enrolled)),
            patch.object(delivery, "wake"),
        ):
            for rejected_origin, error in (
                (project_work, "onboarding-personal-origin-required"),
                (excluded_work, "onboarding-origin-unavailable"),
            ):
                denied_destination = dict(args["destination"], origin=str(rejected_origin))
                rejected = call(dict(args, approved=True, destination=denied_destination))
                assert rejected == {"error": {"code": error}}, rejected
            assert not events
            first = run(dict(args, approved=True))
            (memory / "offline.md").write_text(uncertain)
            lose_next = True
            interrupted = run(dict(args, approved=True))
            (memory / "later.md").write_text("Fictional memory added after the user approved this batch.")
            resumed = run(dict(args, resume=True))
            repeat = run(dict(args, resume=True, approval_id=interrupted["approval_id"]))
        assert first["counts"]["received"] == 1, first
        assert interrupted["counts"]["pending"] == 1, interrupted
        assert interrupted["approval_id"] == resumed["approval_id"] == repeat["approval_id"]
        assert resumed["counts"]["already_exists"] == 2, (resumed, errors, len(events))
        assert repeat["counts"]["already_exists"] == 2, repeat
        assert all(result["counts"]["excluded"] == 1 for result in (first, interrupted, resumed, repeat))
        assert len(events) == 3 and events[1] == events[2]
        for wire in events:
            assert wire["params"]["origin"] == approved_origin
            assert set(wire["params"]["event"]) == {"contract", "event_id", "occurred_at", "content"}
        wire = events[0]
        for params, status_code, code in (
            (dict(wire["params"], origin=collection.discover(excluded_work)), 403, "global_origin_not_approved"),
            (dict(wire["params"], event=dict(wire["params"]["event"], project_id="fictional-project")),
             422, "invalid_memory_contract"),
        ):
            try:
                hub.handle(dict(wire, request_id="fictional_rejected_scope", params=params), principal)
            except Rejected as error:
                assert (error.status, error.code) == (status_code, code), (error.status, error.code)
            else:
                raise AssertionError("Unexpected acceptance of synthetic scope")
        assert identities == [approved_identity] * len(events)
        assert denied not in json.dumps(events)
        assert not any(request["method"] == "content_put" for request in cloud.calls)
        assert all(original not in json.dumps(result) for result in (first, interrupted, resumed, repeat))
        rows = adapter.session_rows(identities[0])
        assert sorted((row["role"], row["content"]) for row in rows) == sorted(
            ("native-memory", text) for text in (original, uncertain)
        )
        event_id = events[0]["params"]["event"]["event_id"]
        params = {"origin": events[0]["params"]["origin"], "session_id": events[0]["params"]["session_id"],
                  "event_id": event_id, "event_only": True}
        status = hub.handle({"request_id": "fictional_l0_readback", "method": "memory_global_status",
                             "params": params}, principal)
        assert status["result"]["capture_extraction"].get("l0_accepted") is True, status
        assert status["result"]["capture_extraction"]["state"] == "unknown", status
        return {"harness": harness, "l0_rows": len(rows), "same_id_replayed": True, "excluded_submissions": 0,
                "approved_personal_namespace": True, "client_rejected_project_route": True,
                "server_rejected_unapproved_origin": True, "server_rejected_per_event_project": True,
                "l0_accepted": True, "initial_call_received": 1,
                "extraction": status["result"]["capture_extraction"]["state"]}
    finally:
        server.doCleanups()
        core.doCleanups()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-root", type=Path, required=True)
    parser.add_argument("--harness", choices=("codex", "codebuddy"), default="codex")
    arguments = parser.parse_args()
    print(json.dumps(verify(arguments.server_root.resolve(), arguments.harness), sort_keys=True))
