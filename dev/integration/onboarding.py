"""Run Client Onboarding against isolated Server routing and the real SQLite Core.

Provide a reviewed Server checkout and its offline Core fixture environment
(AGENTBRAIN_TEST_CORE, TEST_CORE_SOURCE, TEST_NODE). Only fictional auth, sources,
and temporary state are used. The HTTP opener dispatches in process.
"""

import argparse
from contextlib import contextmanager
from dataclasses import replace
import io
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from threading import RLock
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError


def actual_host_prompt(root, workspace, harness, executable, prompt):
    """Capture one real native Hook payload from an isolated CLI workspace."""
    from dev.integration.hook_context import Capture, configure
    from http.server import ThreadingHTTPServer
    from threading import Thread

    fixture = root / "native-host-probe"
    fixture.mkdir()
    home = fixture / "home"
    home.mkdir()
    output = fixture / "event.json"
    witness = fixture / "witness.py"
    witness.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps({"
        "'event': json.load(sys.stdin), 'workspace': os.getenv('CODEBUDDY_PROJECT_DIR'), "
        "'cwd': os.getcwd()}))\nprint('{}')\n", encoding="utf-8",
    )
    command = [sys.executable, "-B", str(witness), str(output)]
    with ThreadingHTTPServer(("127.0.0.1", 0), Capture) as server:
        env, invocation = configure(
            harness, executable, server.server_port, home, workspace,
            subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command),
        )
        server.host, server.requests = harness, []
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            result = subprocess.run([executable, *invocation, prompt], env=env, cwd=workspace,
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=35)
            if result.returncode or not server.requests or not output.exists():
                raise ValueError("Native prompt did not reach both Hook and local model endpoint")
        finally:
            server.shutdown()
    return json.loads(output.read_text(encoding="utf-8"))


def verify(server_root, harness="codex", *, workspace_wire=False, native_executable=None,
           ordinary_workspace=False):
    root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(root), str(root / "src"), str(root / "tests"),
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
    from agent_brain_client.adapters.storage.workspace_identity import host_workspace
    from agent_brain_client.adapters.platforms.filesystem import native_platform
    from agent_brain_client.application.memory.onboarding import SOURCE_SCOPE
    from agent_brain_client.application.memory.observations import stage_messages
    from agent_brain_client.domain.events import WORKSPACE_ORIGIN_CAPABILITY, validate_workspace
    from agent_brain_client.application.queue.delivery import deliver_pending
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
        server.service.source_records.initialize_origins()
        home = server.root / "client"
        home.mkdir()
        work = home / "fictional-work"
        work.mkdir()
        repository = "github.com/synthetic/project-a"
        project = {
            "id": "git-sha256:" + hashlib.sha256(repository.encode()).hexdigest(),
            "repository": repository,
        }
        subprocess.run(["git", "-C", str(work), "init"], check=True, capture_output=True, timeout=10)
        subprocess.run(
            ["git", "-C", str(work), "remote", "add", "origin", "git@github.com:synthetic/project-a.git"],
            check=True, capture_output=True, timeout=10,
        )
        project_work = home / "fictional-project-route"
        (project_work / ".git").mkdir(parents=True)
        excluded_work = home / "fictional-excluded-origin"
        (excluded_work / ".git").mkdir(parents=True)
        native = home / ("." + harness)
        original = "Fictional preference for native SQLite boundary verification."
        uncertain = "Fictional offline native memory approved in the second batch."
        denied = "Fictional oversized original. " * 5000
        if harness == "codex":
            original = (
                "thread_id: 12345678-abcd-1234-abcd-000000000001\n"
                "updated_at: 2026-01-01T00:00:00+00:00\n"
                "rollout_path: /synthetic/unused.jsonl\n"
                f"cwd: {work}\n\n{original}"
            )
        memory = native / ("projects/unknown/memory" if harness == "codebuddy" else "memories/rollout_summaries")
        memory.mkdir(parents=True)
        (memory / "approved.md").write_text(original)
        (memory / "local.md").write_text(denied)

        hub = server.mac
        hub.scope["include_roots"] = [
            {"root": str(work), "sharing": "personal"},
            {"root": str(project_work), "sharing": "project"},
        ]
        hub.scope["exclude_roots"] = [str(excluded_work)]
        if ordinary_workspace:
            hub.scope["all_non_projects"] = True
        hub.owner.update(team_id="fictional-team", user_id="fictional-user")
        hub.value = {"legacy_projects": {}}
        hub.dispatch, hub.recall_handoff = RLock(), RecallHandoff()
        hub.allowance_status = lambda: {"source_admitted": True, "model_admitted": False}
        hub.start = lambda: None  # Proof ends at L0, with no model or background worker.
        hub.directory = server.root / "router"
        sid = hub.scope["profile_id"]
        group = "native-source-" + digest(content_host(harness).binding(home, native))
        approved_origin = collection.discover(work)
        automatic_work = work
        original_prompt = "Fictional automatic capture from Workspace A."
        if native_executable:
            automatic_work = (home if ordinary_workspace else work) / "host-selected-folder"
            automatic_work.mkdir()
            observed = actual_host_prompt(server.root, automatic_work, harness, native_executable, original_prompt)
            if harness == "codebuddy":
                from agent_brain_client.adapters.harnesses.codebuddy.events import session_id
                session = session_id(observed["event"]["session_id"])
            else:
                session = observed["event"]["session_id"]
        else:
            session = "synthetic-auto"
        automatic_origin = collection.discover(automatic_work)
        approved_identity = Identity(
            hub.owner["team_id"], hub.owner["user_id"], hub.owner["device_id"],
            namespace(hub.owner["actor"], sid), _native_session(approved_origin, group),
        )
        automatic_identity = replace(approved_identity, session_id=session)
        core.core.close()
        core.identities = [approved_identity, automatic_identity]
        core.start()
        receipt_dir = hub.directory / "namespaces" / sid / "receipts"
        receipt_dir.mkdir(parents=True, mode=0o700)
        hub.index.write_text(json.dumps({sid: {}}))
        hub.index.chmod(0o600)
        adapter = TencentAdapter(str(receipt_dir / "adapter.sqlite3"), lambda: core.core)
        server.addCleanup(adapter.close)
        events, identities, errors = [], [], []
        workspace_receipts = {}
        lose_next = False

        def native_submit(request, _principal, _client, _write_receipt, *, native_source=False):
            event = request["params"]["event"]
            identity = Identity(hub.owner["team_id"], hub.owner["user_id"], hub.owner["device_id"],
                                event["project_id"], event["session_id"])
            assert identity == (approved_identity if native_source else automatic_identity)
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
            handled = wire
            workspace = wire["params"].get("origin", {}).get("workspace")
            if workspace is not None:
                # Capability fixture only. Forward the exact legacy L0 event to
                # the actual Server/Core; this does not implement the new Server.
                assert workspace_wire and submitted
                validate_workspace(workspace)
                route = dict(wire["params"]["origin"])
                route.pop("workspace")
                handled = dict(wire, params=dict(wire["params"], origin=route))
            try:
                result = server.pool.handle(handled, principal)
            except Exception as exc:
                errors.append((type(exc).__name__, str(exc)))
                raise
            if submitted:
                events.append(wire)
                if workspace is not None:
                    previous = workspace_receipts.setdefault(wire["request_id"], workspace)
                    assert previous == workspace
                if lose_next:
                    lose_next = False
                    raise URLError("Fictional connection loss after the committed response")
            if workspace_wire and wire["method"] == "memory_global_status" and wire["params"] == {}:
                result = dict(result, capabilities=[*result["capabilities"], WORKSPACE_ORIGIN_CAPABILITY])
            return io.BytesIO(json.dumps(result).encode())

        cloud = Cloud()
        cloud.queue_identity = {"device_id": hub.owner["device_id"]}
        cloud.grant["platform"] = native_platform()
        endpoint = "https://synthetic.invalid"
        settings = {"device_home": str(home), "legacy_projects": [], "scope": hub.scope}
        policy = home / "policy.json"
        pin = metadata.write(policy, POLICY)
        profile = home / "https-profile.json"
        profile_pin = metadata.write(profile, {"grant": {"actor": principal["actor"]}})
        enrolled = {"runtime": {"content_policy": str(policy), "content_policy_sha256": pin,
                                "https_profile": str(profile), "https_profile_sha256": profile_pin,
                                "codex_home": str(native), "content_cache": str(home / "content")}}

        def transport(value, origin):
            client = object.__new__(CollectionClient)
            client.value, client.local, client.actor = value, origin, principal["actor"]
            client.device = principal["device_id"]
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
                tasks = [metadata.private_json(path, cache=True)
                         for path in delivery.task_files(delivery.directory(home))]
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
        first_origin = dict(approved_origin, project=project) if harness == "codex" else approved_origin
        onboarding_workspace = host_workspace(str(work), str(work)).source(principal["device_id"])
        if workspace_wire and harness == "codex":
            first_origin = dict(first_origin, workspace=onboarding_workspace)
        for wire, expected_origin in zip(events, (first_origin, approved_origin, approved_origin)):
            assert wire["params"]["origin"] == expected_origin
            assert set(wire["params"]["event"]) == {"contract", "event_id", "occurred_at", "content"}
        wire = events[0]
        if "workspace" in wire["params"]["origin"]:
            try:
                hub.handle(wire, principal)
            except Rejected as error:
                assert (error.status, error.code) == (422, "invalid_global_origin")
            else:
                raise AssertionError("The current Server unexpectedly accepted the new origin field")
            route = dict(wire["params"]["origin"])
            route.pop("workspace")
            wire = dict(wire, params=dict(wire["params"], origin=route))
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
        params = {"origin": approved_origin, "session_id": events[0]["params"]["session_id"],
                  "event_id": event_id, "event_only": True}
        status = hub.handle({"request_id": "fictional_l0_readback", "method": "memory_global_status",
                             "params": params}, principal)
        assert status["result"]["capture_extraction"].get("l0_accepted") is True, status
        assert status["result"]["capture_extraction"]["state"] == "unknown", status
        source_records = server.service.source_records
        assert source_records.origin_project(approved_identity.project_id, event_id, approved_identity.device_id) == (
            project if harness == "codex" else None
        )

        with (
            patch.object(collection, "load", return_value=settings),
            patch.object(collection, "approved", return_value=True),
            patch("agent_brain_client.adapters.server.collection.approved", return_value=True),
            patch.object(collection, "CollectionClient", side_effect=transport),
            patch.object(registration, "registration", return_value=({}, enrolled)),
        ):
            client, queue = collection.build_delivery_queue(settings, automatic_origin, harness)
            if native_executable:
                hooks = dict(observed["event"])
                with (
                    patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(hooks).encode()))),
                    patch.object(os, "getcwd", return_value=observed["cwd"]),
                    patch.object(collection, "schedule_delivery"),
                    patch.object(collection, "recall_prompt", return_value=[]),
                    patch.dict(os.environ, {"CODEBUDDY_PROJECT_DIR": observed["workspace"] or ""}),
                ):
                    collection.hook(settings, harness=harness)
                outbox = list((queue.cache / "outbox").glob("*.json"))
                staged = {"request_ids": [file.stem for file in outbox]}
                assert len(outbox) == 1
                queued = queue._load("outbox/" + outbox[0].name)["request"]
                assert "origin" not in queued["params"]
            else:
                staged = stage_messages(
                    queue, project_id=automatic_identity.project_id, session_id=automatic_identity.session_id,
                    operation_id="synthetic-observation", phase="prompt",
                    messages=[{"role": "user", "content": original_prompt}],
                    workspace=host_workspace(str(automatic_work), str(automatic_work)),
                )
            pending = deliver_pending(queue, allowed=lambda session: client.status(session)["source_admitted"])
            assert not pending, errors
        saved = queue._load("outbox/" + staged["request_ids"][0] + ".json")
        assert saved["state"] == "confirmed"
        auto = events[-1]["params"]
        auto_root = host_workspace(str(automatic_work), str(automatic_work))
        expected_origin = dict(automatic_origin)
        if auto_root.git_project:
            expected_origin["project"] = project
        if workspace_wire:
            expected_origin["workspace"] = auto_root.source(principal["device_id"])
            assert workspace_receipts[staged["request_ids"][0]] == expected_origin["workspace"]
        assert auto["origin"] == expected_origin
        assert source_records.origin_project(approved_identity.project_id, auto["event"]["event_id"],
                                             approved_identity.device_id) == auto_root.git_project
        assert adapter.session_rows(identities[-1])[0]["content"] == original_prompt
        return {"harness": harness, "l0_rows": len(rows), "same_id_replayed": True, "excluded_submissions": 0,
                "native_cli_hook": bool(native_executable), "workspace_contract_shim": workspace_wire,
                "automatic_route_kind": automatic_origin["kind"],
                "automatic_workspace_source": expected_origin.get("workspace"),
                "automatic_git_source": auto_root.git_project is not None,
                "onboarding_workspace_project": "verified" if harness == "codex" else "unknown",
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
    parser.add_argument("--workspace-wire", action="store_true")
    parser.add_argument("--native-executable")
    parser.add_argument("--ordinary-workspace", action="store_true")
    arguments = parser.parse_args()
    print(json.dumps(verify(arguments.server_root.resolve(), arguments.harness,
                            workspace_wire=arguments.workspace_wire, native_executable=arguments.native_executable,
                            ordinary_workspace=arguments.ordinary_workspace),
                     sort_keys=True))
