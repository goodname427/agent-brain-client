"""The same real content contract through each host, plus a forbidden-host runtime."""

import copy
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import URLError

from agent_brain_client.adapters.server.collection import CollectionClient
from agent_brain_client.adapters.storage import metadata, registration
from agent_brain_client.adapters.storage.content_session import ContentSession
from agent_brain_client.application.queue.delivery import deliver_pending
from agent_brain_client.application.memory.guidance import HOST_GUIDANCE, MAPPING_GUIDANCE, ONBOARDING_GUIDANCE
from agent_brain_client.application.memory.observations import stage_messages
from agent_brain_client.application.memory.onboarding import SOURCE_SCOPE
from agent_brain_client.runtime import collection, collection_management
from agent_brain_client.runtime.harness_content import content_host
from agent_brain_client.domain import contract
from agent_brain_client.domain.scopes import digest, namespace, source_id
from agent_brain_client.adapters.platforms.filesystem import private_directory
from agent_brain_client.domain.errors import CacheBlocked
from agent_brain_client.interfaces.mcp.content import ContentServer, ManagedContentServer
from agent_brain_client.interfaces.mcp.framing import ContentParameterError, ProtocolError
from test_content_outbox import (
    Cloud, POLICY, MEMORY, RULE, SKILL, seed_legacy_native_plan, seed_legacy_native_review,
    seed_legacy_native_write,
)

ROOT = Path(__file__).resolve().parents[1]


class SyntheticCaptureServer:
    def __init__(self):
        self.requests = []
        self.receipts = {}
        self.lose_ack = True
        self.admitted = True
        self.extraction = "staged"

    def open(self, request, **_):
        body = json.loads(request.data)
        self.requests.append(body)
        params = body["params"]
        key = (digest(params["origin"]), params["session_id"])
        if body["method"] == "memory_global_status":
            if params.get("event_only"):
                accepted = (*key, params["event_id"]) in self.receipts
                result = {"capture_extraction": {
                    "l0_accepted": accepted, "state": self.extraction if accepted else "unknown",
                }}
            else:
                result = {"source_admitted": self.admitted, "global_profile_id": "a" * 32,
                          "scope_sha256": self.scope_sha}
            persistence = {"state": "read-only", "durable": False}
        else:
            assert body["method"] == "memory_global_event_submit"
            event = params["event"]
            if event["contract"] == "agentbrain.native-memory.v1":
                assert set(event) == {"contract", "event_id", "occurred_at", "content"}
                count = 1
            else:
                assert event["contract"] == "agentbrain.event.v1"
                count = len(event["messages"])
            key = (*key, event["event_id"])
            replayed = key in self.receipts
            if replayed:
                assert self.receipts[key] == body
            self.receipts[key] = body
            if self.lose_ack:
                self.lose_ack = False
                raise URLError("Synthetic lost reply")
            result = {
                "event_id": event["event_id"], "state": "committed",
                "message_ids": [f"synthetic-l0-{n}" for n in range(count)], "ledger_sync": "sqlite-wal-full",
                "core_acceptance": "acknowledged-not-durability-proof",
                "pipeline_state": "unverified", "model_extraction_verified": False,
                "capture_extraction": {"state": self.extraction}, "replayed": replayed,
            }
            persistence = {
                "state": "core-accepted", "durable": False, "github_confirmed": False,
                "receipt_ledger_durable": True,
            }
        return io.BytesIO(json.dumps({
            "request_id": body["request_id"], "result": result, "persistence": persistence,
        }).encode())


class NativeMemoryCapture(unittest.TestCase):
    def exercise(self, home, harness, windows_layout):
        home.mkdir()
        workspace = home / "synthetic-workspace"
        workspace.mkdir()
        native = home / ("." + harness)
        source_file = native / "memories/example.md"
        original = "合成记忆\r\nA fictional preference.\r\n"
        source_epoch = 1234567890123000000
        peer = SyntheticCaptureServer()
        cloud = Cloud()
        cloud.queue_identity = {"device_id": "synthetic"}
        value = {
            "device_home": str(home), "legacy_projects": [],
            "scope": {
                "schema": 1, "profile_id": "a" * 32,
                "include_roots": [], "exclude_roots": [], "non_project_roots": [],
                "recall_personal": True, "all_non_projects": True,
            },
        }
        peer.scope_sha = digest(value["scope"])
        policy = home / "fixture-policy.json"
        metadata.write(policy, POLICY)
        enrolled = {"runtime": {
            "content_policy": str(policy),
            "content_policy_sha256": metadata.digest(policy.read_bytes()),
            "codex_home": str(home / "synthetic-shared-native"),
        }}

        def transport(value, origin):
            client = object.__new__(CollectionClient)
            client.value, client.local, client.actor = value, origin, "synthetic-owner"
            client.base = SimpleNamespace(
                queue_identity=cloud.queue_identity,
                _credential=lambda: "synthetic-credential",
                profile={"endpoint": "https://synthetic.invalid"},
                transport=SimpleNamespace(opener=peer),
            )
            client.grant, client.queue_identity = cloud.grant, dict(cloud.queue_identity, origin_sha=digest(origin))
            client.scope_sha = peer.scope_sha
            return client

        def server():
            session = ContentSession(home, home / "content", POLICY, cloud,
                                     host=content_host(harness), native_home=native)
            with patch.object(session, "platform", "win32-x64" if windows_layout else "darwin-arm64"):
                session.sources.provider = session.host.sources(session)
            return ManagedContentServer(session, "0.0.0", "initial")

        def tool(worker, name, arguments):
            return worker.invoke(name, arguments)["structuredContent"]

        with (
            patch.object(collection, "load", return_value=value),
            patch.object(collection, "approved", return_value=True),
            patch("agent_brain_client.adapters.server.collection.approved", return_value=True),
            patch.object(collection, "CollectionClient", side_effect=transport),
            patch.object(registration, "registration", return_value=({}, enrolled)),
            patch("os.getcwd", return_value=str(home)),
        ):
            worker = server()
            initialized = worker.handle({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                           "clientInfo": {"name": "synthetic", "version": "1"}},
            })
            self.assertIn(ONBOARDING_GUIDANCE, initialized["instructions"])
            self.assertIn("call memory_onboard once", ONBOARDING_GUIDANCE)
            worker.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools = {item["name"]: item for item in worker.handle({
                "jsonrpc": "2.0", "id": 2, "method": "tools/list",
            })["tools"]}
            self.assertIn("memory_onboard", tools)
            self.assertFalse(tools.keys() & {
                "content_sources_inventory", "content_sources_read", "content_sources_review",
                "content_sources_import", "content_sources_stage", "memory_global_event_submit",
                "memory_event_submit", "memory_global_status",
            })
            args = {
                "harness": harness,
                "destination": {"endpoint": "https://synthetic.invalid", "actor": "synthetic-owner",
                                "origin": str(workspace)},
                "approved": True,
                "source_scope": SOURCE_SCOPE,
            }
            missing = tool(worker, "memory_onboard", args)
            self.assertEqual(missing["inventory"], "unavailable")
            source_file.parent.mkdir(parents=True)
            source_file.write_bytes(original.encode())
            os.utime(source_file, ns=(source_epoch, source_epoch))
            protected = {
                "private.md": "[LocalOnly] fictional protected material",
                "credential.md": "password: synthetic-password-12345",
                "variables.md": 'EXAMPLE_SECRET_ACCESS_KEY="synthetic-credential-fixture"',
                "confidential.md": "confidential: fictional unavailable material",
                "oversized.md": "合" * 8193,
            }
            for filename, raw in protected.items():
                source_file.with_name(filename).write_bytes(raw.encode())
            source_file.with_name("unapproved.md").write_bytes(b"sharing: unverified\nAn unapproved synthetic preference.")
            if windows_layout and harness == "codebuddy":
                project = native / "projects/unverified/memory/topic.md"
                project.parent.mkdir(parents=True)
                project.write_bytes(b"An isolated Windows CodeBuddy project synthetic preference.")

            sources = worker.adapter.sources
            legacy_excluded = "Fictional original with a retained explicit exclusion."
            source_file.with_name("explicitly-excluded.md").write_bytes(legacy_excluded.encode())
            rows = {
                item["source_fingerprint"]: item
                for item in sources.provider.inventory(onboarding=True)["sources"]
            }
            excluded_row = rows[sha256(legacy_excluded.encode()).hexdigest()]
            seed_legacy_native_plan(worker.adapter, excluded_row, MEMORY)
            seed_legacy_native_review(
                worker.adapter, excluded_row, "excluded", "secret-localonly-or-confidential",
            )
            original_row = rows[sha256(original.encode()).hexdigest()]
            if windows_layout:
                key = seed_legacy_native_plan(worker.adapter, original_row, MEMORY)
                request = {
                    "request_id": "synthetic-legacy-cas-onboarding",
                    "method": "content_put",
                    "params": dict(kind="memory", id="synthetic-topic", record=MEMORY,
                                   expected_revision="missing"),
                }
                reconciles = seed_legacy_native_write(worker.adapter, key, request)
                if harness == "codex":
                    saved = worker.adapter.invoke(request, reconciles=reconciles)
                    self.assertEqual(saved["local_queue"]["state"], "confirmed")
            else:
                seed_legacy_native_review(
                    worker.adapter, original_row, "shareable", "safe-cross-project-personal-summary",
                )
            legacy_ledgers = {
                filename: worker.adapter._load(filename)
                for filename in ("memory-reviews.json", "imports.json")
            }
            cloud.calls.clear()

            scopes = cloud.grant["scopes"]
            cloud.grant["scopes"] = ["read"]
            self.assertNotIn("memory_onboard", {item["name"] for item in worker.catalog()})
            with patch.object(worker.adapter.sources.provider, "inventory",
                              side_effect=AssertionError("Grants must gate source enumeration")):
                with self.assertRaises(ContentParameterError):
                    tool(worker, "memory_onboard", args)
            cloud.grant["scopes"] = scopes
            with self.assertRaises(ContentParameterError):
                tool(worker, "memory_onboard", dict(args, approved=False))
            wrong_destination = dict(args["destination"], actor="other-owner")
            self.assertEqual(tool(worker, "memory_onboard", dict(args, destination=wrong_destination)),
                             {"error": {"code": "onboarding-destination-changed"}})
            with patch("agent_brain_client.runtime.onboarding.source_active", return_value=False):
                self.assertEqual(tool(worker, "memory_onboard", args)["error"]["code"],
                                 "onboarding-personal-origin-required")
            self.assertEqual(peer.requests, [])
            peer.admitted = False
            denied = tool(worker, "memory_onboard", args)
            self.assertEqual(denied["reasons"].get("server-source-paused-or-denied"), 1, denied)
            self.assertEqual([r["method"] for r in peer.requests], ["memory_global_status"])
            peer.requests.clear()
            peer.admitted = True

            queued = tool(worker, "memory_onboard", args)
            self.assertEqual(queued["counts"]["pending"], 1, queued)
            self.assertEqual(queued["counts"]["excluded"], len(protected) + 2 + (windows_layout and harness == "codebuddy"))
            self.assertEqual(queued["reasons"]["private-or-sensitive"], 4)
            self.assertEqual(queued["reasons"]["shareability-unconfirmed"], 1)
            self.assertEqual(queued["reasons"]["retained-exclusion"], 1)
            if windows_layout and harness == "codebuddy":
                self.assertEqual(queued["reasons"]["project-origin-unverified"], 1)
            encoded = json.dumps(queued, ensure_ascii=False)
            for raw in [original, legacy_excluded, *protected.values(), str(workspace)]:
                self.assertNotIn(raw, encoded)
            self.assertEqual(len(peer.receipts), 1)
            admission, initial_wire = peer.requests
            self.assertEqual(admission["method"], "memory_global_status")
            self.assertEqual(initial_wire["params"]["event"]["content"], original)
            self.assertEqual(initial_wire["params"]["event"]["occurred_at"], "2009-02-13T23:31:30.123Z")
            original_origin = initial_wire["params"]["origin"]
            _, imported_queue = collection.build_delivery_queue(value, original_origin, harness)
            # Ordinary Hook drains cannot send an explicit historical request.
            self.assertFalse(deliver_pending(imported_queue, allowed=lambda _: True))
            self.assertEqual(len(peer.requests), 2)

            # First use of this origin was Onboarding. Flush must also reach a
            # pending Hook capture from a different origin of the same Harness.
            other_workspace = home / "synthetic-other-workspace"
            other_workspace.mkdir()
            other_origin = collection.eligible(value, str(other_workspace))
            hook_client, hook_queue = collection.build_delivery_queue(value, other_origin, harness)
            hook = stage_messages(
                hook_queue, project_id=namespace(
                    hook_client.actor, source_id(value["scope"], other_origin, "personal"),
                ),
                session_id="synthetic-hook-session", operation_id="synthetic-hook-operation",
                phase="prompt", messages=[{"role": "user", "content": "Fictional Hook capture."}],
            )
            hook_rid = hook["request_ids"][0]
            self.assertEqual(hook_queue.queue_status()["pending"], 1)
            private_directory(hook_queue.cache.parent / ("f" * 64))
            flushed = collection_management.flush(value, services=collection.native_services())
            self.assertEqual(flushed["hosts"][harness]["status"], "global-original-queue-sync")
            self.assertEqual(hook_queue._load("outbox/" + hook_rid + ".json")["state"], "confirmed")
            self.assertEqual(imported_queue.queue_status()["pending"], 1)
            self.assertEqual(metadata.private_json(imported_queue.cache / "origin.json", cache=True), original_origin)

            worker = server()
            calls = len(peer.requests)
            peer.extraction = "no-facts"
            resume = {key: value for key, value in args.items() if key != "approved"}
            resume["resume"] = True
            source_file.with_name("after-approval.md").write_text("A later fictional memory outside this approval.")
            done = tool(worker, "memory_onboard", resume)
            self.assertEqual(done["approval_id"], queued["approval_id"])
            self.assertEqual(done["counts"]["already_exists"], 1, done)
            self.assertEqual(done["extraction"], {"no-facts": 1})
            self.assertIn(initial_wire, peer.receipts.values())
            self.assertEqual(peer.requests[-1], initial_wire)
            self.assertEqual(len(peer.requests), calls + 2)
            self.assertEqual(len(peer.receipts), 2)
            alternate = dict(args["destination"], origin=str(other_workspace))
            wrong_resume = tool(worker, "memory_onboard", dict(resume, destination=alternate,
                                approval_id=queued["approval_id"]))
            self.assertEqual(wrong_resume["error"]["code"], "onboarding-approval-unavailable")
            source_file.with_name("after-approval.md").unlink()
            existing = tool(worker, "memory_onboard", dict(args, destination=alternate))
            self.assertEqual(existing["counts"]["already_exists"], 1)
            self.assertEqual(len(peer.requests), calls + 2)  # no rebind or disclosure
            repeated = tool(worker, "memory_onboard", resume)
            self.assertEqual(repeated["counts"]["already_exists"], 1)
            self.assertEqual(peer.requests[-1]["method"], "memory_global_status")
            self.assertEqual(len(peer.receipts), 2)
            self.assertEqual(cloud.calls, [])
            for filename, expected in legacy_ledgers.items():
                self.assertEqual(worker.adapter._load(filename), expected)
            self.assertNotIn(legacy_excluded, json.dumps(peer.requests))
            if harness == "codex" and not windows_layout:
                for index in range(70):
                    raw = f"Fictional onboard batch item {index}".encode()
                    source_file.with_name(f"batch-{index}.md").write_bytes(raw)
                batch = tool(worker, "memory_onboard", args)
                self.assertEqual(batch["counts"]["received"], 70, batch)
                self.assertEqual(batch["counts"]["already_exists"], 1)
                submitted = [row for row in peer.requests if row["method"] == "memory_global_event_submit"]
                self.assertEqual(len(submitted), 73)
                self.assertEqual(len(peer.receipts), 72)
                self.assertLess(len(json.dumps(batch)), 1024)

    def test_first_origin_import_preserves_other_pending_capture_and_exact_replay(self):
        for harness in ("codex", "codebuddy"):
            for windows_layout in (False, True):
                with self.subTest(harness=harness, windows_layout=windows_layout), tempfile.TemporaryDirectory() as tmp:
                    self.exercise(Path(tmp).resolve() / "home", harness, windows_layout)


class HarnessParity(unittest.TestCase):
    def test_both_harnesses_read_exact_approved_original_and_file_time(self):
        original = "Synthetic memory ☃\r\nSecond line.\r\n"
        for harness in ("codex", "codebuddy"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp).resolve()
                cloud = Cloud()
                native = home / ("." + harness)
                source = native / "memories/synthetic.md"
                source.parent.mkdir(parents=True)
                source.write_bytes(original.encode())
                os.utime(source, ns=(1234567890000000000, 1234567890000000000))
                session = ContentSession(
                    home, home / "cache", POLICY, cloud,
                    host=content_host(harness), native_home=native,
                )
                row = session.sources.provider.inventory()["sources"][0]
                selected = (row["source_id"], row["source_fingerprint"])
                self.assertEqual(session.sources.read_onboarding_original(*selected),
                                 (original, source.stat().st_mtime_ns))
                source.write_bytes(b"Synthetic changed source")
                with self.assertRaises(CacheBlocked):
                    session.sources.read_onboarding_original(*selected)
                self.assertEqual(cloud.calls, [])

    def test_onboarding_exclusions_leave_legacy_cas_and_its_recovery_untouched(self):
        for acknowledgement_lost in (False, True):
            with self.subTest(acknowledgement_lost=acknowledgement_lost), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp).resolve()
                native = home / ".codex"
                source = native / "memories/synthetic.md"
                source.parent.mkdir(parents=True)
                source.write_bytes(b"Synthetic durable source")
                cloud = Cloud()
                session = ContentSession(
                    home, home / "cache", POLICY, cloud,
                    host=content_host("codex"), native_home=native,
                )
                row = session.sources.provider.inventory()["sources"][0]
                selected = (row["source_id"], row["source_fingerprint"])
                session.sources.read_onboarding_original(*selected)
                key = seed_legacy_native_plan(session, row, MEMORY)
                cloud.lose_ack = acknowledgement_lost
                request = {
                    "request_id": "synthetic-legacy-source-cas",
                    "method": "content_put",
                    "params": dict(kind="memory", id="synthetic-topic", record=MEMORY,
                                   expected_revision="missing"),
                }
                reconciles = seed_legacy_native_write(session, key, request)
                session.invoke(request, reconciles=reconciles)
                calls = len(cloud.calls)
                with patch.object(session, "_queued", side_effect=AssertionError("Legacy CAS read")):
                    self.assertEqual(session.sources.onboarding_exclusions(), set())
                self.assertEqual(len(cloud.calls), calls)
                if acknowledgement_lost:
                    replay = session.invoke(request, reconciles=reconciles)
                    self.assertEqual(replay["local_queue"]["state"], "confirmed")
                    self.assertEqual(cloud.puts, 1)

    def test_same_content_cas_queue_offline_and_mapping_contract(self):
        outcomes = []
        for harness in ("codex", "codebuddy"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp).resolve()
                cloud = Cloud()
                cloud.queue_identity = {"service": "synthetic", "host_adapter": harness}
                cloud.records = {("rule", "synthetic-rule"): dict(RULE, enabled=True, trigger={"mode": "always", "patterns": []}),
                                 ("skill", "synthetic-skill"): dict(SKILL, enabled=True)}
                adapter = ContentSession(home, home / "cache", copy.deepcopy(POLICY), cloud,
                                         host=content_host(harness), native_home=home / (".codex" if harness == "codex" else ".codebuddy"))
                self.assertEqual(adapter.refresh()["status"], "content-refreshed")
                skill = home / (".agents" if harness == "codex" else ".codebuddy") / "skills/synthetic-skill/SKILL.md"
                self.assertEqual(skill.read_text(), SKILL["files"]["SKILL.md"])
                request = {"request_id": "synthetic-parity-write", "method": "content_put", "params": {
                    "kind": "memory", "id": "synthetic-topic", "expected_revision": "missing", "record": MEMORY}}
                result = adapter.invoke(request)
                self.assertTrue(result["persistence"]["durable"])
                self.assertEqual(adapter.invoke(request)["local_queue"]["state"], "confirmed")
                cloud.offline = True
                read = adapter.invoke({"request_id": "synthetic-offline-read", "method": "content_read",
                                       "params": {"kind": "memory", "id": "synthetic-topic"}})
                self.assertTrue(read["local_cache"]["stale"])
                outcomes.append((result["local_queue"]["state"], read["result"]["record"], adapter.queue_status()))
                if harness == "codebuddy":
                    self.assertFalse((home / ".codex").exists())
                    self.assertFalse((home / ".agents").exists())
                    self.assertIn("alwaysApply: true", (home / ".codebuddy/rules/agent-brain-synthetic-rule.md").read_text())
                    index = home / ".codebuddy/rules/agent-brain-client/index.md"
                else:
                    index = home / ".codex/AGENTS.md"
                    self.assertIn(HOST_GUIDANCE, index.read_text())
                self.assertIn(MAPPING_GUIDANCE, index.read_text())
        self.assertEqual(outcomes[0], outcomes[1])

    def test_codebuddy_ide_memory_uses_observed_host_directory_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            adapter = ContentSession(home, home / "cache", POLICY, Cloud(), host=content_host("codebuddy"), native_home=home / ".codebuddy")
            source = adapter.sources.provider.ide / "synthetic.mdc"
            source.parent.mkdir(parents=True)
            source.write_text("---\ntitle: Synthetic IDE memory\n---\nA synthetic preference")
            row = adapter.sources.provider.inventory()["sources"][0]
            self.assertEqual(adapter.sources.read_onboarding_original(row["source_id"], row["source_fingerprint"])[0], source.read_bytes().decode())
            self.assertFalse((home / ".codex").exists())

    def test_shared_session_requires_explicit_host(self):
        with self.assertRaises(TypeError):
            ContentSession("synthetic", "cache", POLICY, Cloud())

    def test_codebuddy_source_review_import_and_foreign_source_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            native = home / ".codebuddy"
            source = native / "memories/global/preferences.md"
            source.parent.mkdir(parents=True)
            source.write_text("Synthetic preference only")
            adapter = ContentSession(home, home / "cache", POLICY, Cloud(), host=content_host("codebuddy"), native_home=native)
            from agent_brain_client.adapters.harnesses.codebuddy import configuration
            from agent_brain_client.adapters.storage.host_files import host_plan
            from agent_brain_client.adapters.storage.metadata import write

            instructions = native / "CODEBUDDY.md"
            prior = configuration.BEGIN + "\nSynthetic prior instructions\n" + configuration.END
            journal = private_directory(configuration.state_directory(home)) / "transaction.json"
            write(
                journal,
                {"schema": 1, "phase": "complete", "host": host_plan({instructions: prior.encode()})},
            )
            user_id = "codebuddy-memory:" + sha256(b"CODEBUDDY.md").hexdigest()
            for block in (prior, configuration.GUIDANCE):
                for newline in (b"\n", b"\r\n"):
                    with self.subTest(current=block == configuration.GUIDANCE, newline=newline):
                        managed = block.encode().replace(b"\n", newline)
                        instructions.write_bytes(managed + newline)
                        rows = adapter.sources.provider.inventory()["sources"]
                        self.assertEqual(len(rows), 1)
                        self.assertNotEqual(rows[0]["source_id"], user_id)

                        prefix = "合成偏好。  ".encode() + newline * 2
                        suffix = newline + b"Synthetic personal note.  " + newline
                        data = prefix + managed + suffix
                        instructions.write_bytes(data)
                        rows = adapter.sources.provider.inventory()["sources"]
                        user = next(row for row in rows if row["source_id"] == user_id)
                        self.assertEqual(user["source_fingerprint"], sha256(prefix + suffix).hexdigest())
                        read = adapter.sources.read_onboarding_original(user_id, user["source_fingerprint"])
                        self.assertEqual(read[0].encode(), prefix + suffix)
                        self.assertEqual(instructions.read_bytes(), data)

                        edited = managed.replace(configuration.END.encode(), b"User note. " + configuration.END.encode())
                        instructions.write_bytes(edited)
                        rows = adapter.sources.provider.inventory()["sources"]
                        user = next(row for row in rows if row["source_id"] == user_id)
                        self.assertEqual(user["source_fingerprint"], sha256(edited).hexdigest())
            instructions.write_bytes(configuration.GUIDANCE.encode() + b"\n")
            inventory = adapter.sources.provider.inventory()
            row = inventory["sources"][0]
            self.assertTrue(row["source_id"].startswith("codebuddy-memory:"))
            self.assertEqual(adapter.sources.read_onboarding_original(row["source_id"], row["source_fingerprint"])[0], source.read_bytes().decode())
            with self.assertRaises(CacheBlocked):
                adapter.sources.read_onboarding_original("codex-memory:" + "a" * 64, "a" * 64)

    def test_codebuddy_only_import_and_real_mcp_refresh_without_codex(self):
        code = '''
import importlib.abc, sys, pathlib, tempfile, copy
sys.path[:0] = [sys.argv[1], sys.argv[2], str(pathlib.Path(sys.argv[1]).parent)]
class NoCodex(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('agent_brain_client.adapters.harnesses.codex'):
            raise AssertionError('CodeBuddy loaded Codex: ' + fullname)
sys.meta_path.insert(0, NoCodex())
from agent_brain_client.runtime import collection, updates, host_binding
from agent_brain_client.adapters.installation import installation
from agent_brain_client.interfaces.mcp.content import ContentServer
from agent_brain_client.interfaces.management.server import installed_adapter
from agent_brain_client.adapters.storage.content_session import ContentSession
from agent_brain_client.runtime.harness_content import content_host
POLICY = {'schema':1,'adapter':'codex','kinds':['memory','rule','skill'],'ids':{'memory':'*','rule':['synthetic-rule'],'skill':['synthetic-skill']},'dependencies':{'commands':[],'python_modules':[]}}
class Cloud:
    grant = {'scopes':['read','put'],'artifact_types':POLICY['kinds'],'ids':POLICY['ids']}
    def request(self, request):
        return {'request_id':request['request_id'], 'result':{'content_api_version':1,'commit':'a'*40,'artifacts':[]}}
with tempfile.TemporaryDirectory() as tmp:
    home = pathlib.Path(tmp).resolve()
    adapter = ContentSession(home, home/'cache', copy.deepcopy(POLICY), Cloud(), host=content_host('codebuddy'), native_home=home/'.codebuddy')
    server = ContentServer(adapter)
    response = server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'synthetic','version':'1'}}})
    assert response['serverInfo']['name'] == 'agent-brain-content', response
    assert not (home/'.codex').exists()
    assert not (home/'.agents').exists()
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT / "src"), str(ROOT / "tests")], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


class WindowsCodeBuddySources(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.native = self.home / ".codebuddy"
        self.cloud = Cloud()
        self.adapter = self.restart()
        self.server = ContentServer(self.adapter)

    def restart(self):
        adapter = ContentSession(self.home, self.home / "cache", POLICY, self.cloud,
                                 host=content_host("codebuddy"), native_home=self.native)
        if os.name != "nt":
            # Select Windows layouts while exercising the local platform's real I/O.
            with patch.object(adapter, "platform", "win32-x64"):
                adapter.sources.provider = adapter.host.sources(adapter)
        return adapter

    def write_source(self, relative, *, root=None):
        file = (self.native if root is None else root) / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(f"Synthetic fixture for {relative}".encode())
        return file

    def source_id(self, relative):
        return "codebuddy-memory:" + sha256(relative.encode()).hexdigest()

    def inventory(self):
        return self.adapter.sources.provider.inventory()

    def read_original(self, row):
        return self.adapter.sources.read_onboarding_original(
            row["source_id"], row["source_fingerprint"],
        )[0]

    def link_directory(self, link, target):
        if os.name == "nt":
            result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.addCleanup(link.rmdir)
        else:
            link.symlink_to(target, target_is_directory=True)

    def test_new_layouts_without_old_roots_and_metadata_are_filtered(self):
        sources = {
            path: self.write_source(path) for path in (
                "memery/全局_memery.md",
                "projects/示例项目/memory/topic.md",
                "projects/second-project/memory/topic.md",
            )
        }
        for path in (
            "memery/user-memery-state.json", "memery/notes.md", "memery/archive_memery.md.bak",
            "projects/示例项目/memory/MEMORY.md", "projects/示例项目/memory/.ide-migrated.json",
            "projects/示例项目/memory/session.jsonl", "projects/示例项目/session.jsonl",
            "projects/示例项目/sessions/memory.md", "projects/示例项目/memory/history/turn.md",
            "sessions/other-project_memery.md",
        ):
            self.write_source(path)
            with self.assertRaises(CacheBlocked):
                self.adapter.sources.provider.read(self.source_id(path))

        first = self.inventory()
        self.assertEqual(first["status"], "complete")
        self.assertEqual(first, self.inventory())
        self.assertEqual(first["counts"], {"not-checked": 3})
        expected = {self.source_id(path): file.read_bytes() for path, file in sources.items()}
        self.assertEqual({row["source_id"] for row in first["sources"]}, set(expected))
        for row in first["sources"]:
            raw = expected[row["source_id"]]
            self.assertEqual(row["source_fingerprint"], sha256(raw).hexdigest())
            self.assertEqual(self.read_original(row), raw.decode())
        self.assertFalse(self.cloud.calls)

    def test_old_layouts_can_coexist_and_keep_existing_ids(self):
        paths = {
            "cli/global/preferences.md": self.write_source("memories/global/preferences.md"),
            "ide/synthetic.mdc": self.write_source("synthetic.mdc", root=self.adapter.sources.provider.ide),
            "CODEBUDDY.md": self.write_source("CODEBUDDY.md"),
        }
        self.write_source("memories/global/MEMORY.md")
        self.write_source(".memory-global-config.json", root=self.adapter.sources.provider.ide)
        self.write_source("sessions/turn.md", root=self.adapter.sources.provider.ide)
        before = {row["source_id"]: row for row in self.inventory()["sources"]}
        self.assertEqual(set(before), {self.source_id(path) for path in paths})
        self.write_source("memery/global_memery.md")
        self.write_source("projects/example/memory/topic.md")
        after = {row["source_id"]: row for row in self.inventory()["sources"]}
        self.assertEqual(len(after), len(before) + 2)
        for path, file in paths.items():
            source_id = self.source_id(path)
            self.assertEqual(before[source_id], after[source_id])
            self.assertEqual(self.read_original(after[source_id]), file.read_bytes().decode())

    def test_linked_project_memory_is_not_read_and_linked_root_is_incomplete(self):
        self.write_source("memery/global_memery.md")
        project = self.write_source("projects/example/memory/topic.md")
        original = self.inventory()
        selected = next(row for row in original["sources"]
                        if row["source_id"] == self.source_id("projects/example/memory/topic.md"))
        outside = self.home / "outside"
        project.parent.rename(outside)
        self.link_directory(project.parent, outside)
        with self.assertRaises(CacheBlocked):
            self.read_original(selected)
        self.assertEqual(self.inventory()["status"], "complete")
        self.link_directory(self.native / "memories", outside)
        result = self.inventory()
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["counts"], {"not-checked": 1, "excluded": 1})
        self.assertEqual({row["source_id"] for row in result["sources"] if row["source_fingerprint"]},
                         {self.source_id("memery/global_memery.md")})

    def test_onboarding_excludes_unverified_project_origin_without_reading_originals(self):
        local = self.write_source("projects/example/memory/private.md")
        personal = self.write_source("memery/fixture_memery.md")
        result = self.adapter.sources.provider.inventory(onboarding=True)
        rows = {row["source_id"]: row for row in result["sources"]}
        project = rows[self.source_id("projects/example/memory/private.md")]
        self.assertEqual(project["reason"], "project-origin-unverified")
        self.assertIsNone(project["source_fingerprint"])
        self.assertNotIn(local.read_bytes().decode(), json.dumps(result))
        personal_row = rows[self.source_id("memery/fixture_memery.md")]
        self.assertEqual(self.read_original(personal_row), personal.read_bytes().decode())
        self.assertEqual(self.cloud.calls, [])


class QueueParity(unittest.TestCase):
    def test_manual_sync_drains_both_original_queues_and_rechecks_admission(self):
        from types import SimpleNamespace
        from agent_brain_client.runtime.capture_queues import open_queue
        from agent_brain_client.runtime.collection_management import flush
        from agent_brain_client.application.memory.observations import stage_messages
        from agent_brain_client.adapters.storage import metadata, registration
        from test_event_hook import accepted
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            policy = home / "policy.json"
            metadata.write(policy, POLICY)
            value = {"device_home": str(home), "scope": {"synthetic": True}}
            local = {"kind": "non-project", "cwd": str(home), "project_root": None, "repository_root": None}
            sent = []
            class Client:
                grant = {"scopes": ["read", "put"], "artifact_types": POLICY["kinds"], "ids": POLICY["ids"]}
                def __init__(self):
                    self.queue_identity = {"service": "synthetic"}
                def request(self, request):
                    sent.append(copy.deepcopy(request))
                    return accepted(request)
                def status(self, session):
                    return {"source_admitted": True}
            services = SimpleNamespace(CollectionClient=lambda *a: Client(), approved=lambda value: True)
            services.build_delivery_queue = lambda value, local, harness: open_queue(value, local, harness, services=services)
            enrolled = {"runtime": {"content_policy": str(policy), "content_policy_sha256": metadata.digest(policy.read_bytes()),
                                     "codex_home": str(home / "shared-native")}}
            with patch.object(registration, "registration", return_value=({}, enrolled)), patch("agent_brain_client.runtime.collection_management.select", return_value={"sharing": "cross-project"}):
                originals = []
                for harness in ("codex", "codebuddy"):
                    _, queue = services.build_delivery_queue(value, local, harness)
                    stage_messages(queue, project_id="synthetic-project", session_id="synthetic-session", operation_id="synthetic-turn",
                                   phase="prompt", messages=[{"role": "user", "content": "Synthetic future observation"}])
                    originals.extend(row["request"] for row in queue._queued())
                self.assertFalse(sent)
                result = flush(value, services=services)
                self.assertEqual(result["status"], "global-original-queue-sync")
                self.assertEqual(set(result["hosts"]), {"codex", "codebuddy"})
                self.assertEqual(sorted(sent, key=lambda row: row["request_id"]), sorted(originals, key=lambda row: row["request_id"]))
                sent.clear()
                flush(value, services=services)
                self.assertFalse(sent)
                self.assertFalse((home / ".codex").exists())
                self.assertFalse((home / ".agents").exists())

    @unittest.skipUnless(sys.platform == "darwin", "Native Mac installation contract")
    def test_codebuddy_only_verified_install_source_setup_and_binding(self):
        code = '''
import sys, pathlib, importlib.abc
sys.path[:0] = [sys.argv[1], sys.argv[2], str(pathlib.Path(sys.argv[1]).parent)]
import test_mac_first_install as fixture
f = fixture.FirstInstall('test_first_install_prepared_environment_and_exact_native_transaction')
f.setUp()
try:
    f.fixture()
    f.harness = 'codebuddy'
    f.native = f.home / '.config/agent-brain-client/shared-native'
    f.native.mkdir()
    f.config = f.native / 'config.toml'
    for name in list(sys.modules):
        if name.startswith('agent_brain_client.adapters.harnesses.codex'):
            del sys.modules[name]
    class NoCodex(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith('agent_brain_client.adapters.harnesses.codex'):
                raise AssertionError('CodeBuddy installation loaded Codex: ' + fullname)
    sys.meta_path.insert(0, NoCodex())
    result = f.run_wizard()
    assert result['installed'], result
    from types import SimpleNamespace
    from agent_brain_client.runtime.host_maintenance import install_sources
    from agent_brain_client.runtime.host_binding import load_core, backend
    from agent_brain_client.adapters.harnesses.codebuddy.configuration import install, status
    install_sources(SimpleNamespace(device_home=str(f.home), profile_id=None, operator=None, operator_sha256=None))
    core = load_core(f.home)
    from unittest.mock import patch
    with patch("agent_brain_client.adapters.installation.launcher_update.prepare_native_hook"):
        install(f.home, pathlib.Path(sys.argv[1]).parent, core, surface='both')
    from agent_brain_client.adapters.installation.installation import inspect
    from unittest.mock import patch
    with patch("agent_brain_client.adapters.packages.client.ClientReleases.verified_active", return_value={"version":"synthetic-fixture"}):
        checked = inspect(f.home)
        assert status(f.home, core, backend_check=backend)["installed"]
    assert checked.get('installed'), checked
    assert not (f.home/'.codex').exists()
    assert not (f.home/'.agents').exists()
    assert not any(name.startswith('agent_brain_client.adapters.harnesses.codex') for name in sys.modules)
finally:
    f.doCleanups()
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT / "src"), str(ROOT / "tests")], capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


class ProjectParity(unittest.TestCase):
    @patch("agent_brain_client.adapters.installation.launcher_update.prepare_native_hook")
    def test_project_settings_paths_and_native_hooks_are_host_isolated(self, prepare):
        from agent_brain_client.adapters.storage.project_settings import project_settings
        from agent_brain_client.adapters.harnesses.codebuddy.project_configuration import install
        from agent_brain_client.adapters.harnesses.codebuddy.configuration import Document
        from agent_brain_client.adapters.storage import metadata
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            project = home / "work"
            project.mkdir()
            codex = project_settings(home, project, harness="codex")
            buddy = project_settings(home, project, harness="codebuddy")
            self.assertNotEqual(codex, buddy)
            private_directory(buddy.parent)
            metadata.write(buddy, {"synthetic": True})
            settings = {"harness": "codebuddy", "device_home": str(home),
                        "project_root": str(project), "source_mode": "recall-only"}
            config = project / ".codebuddy/settings.local.json"
            config.parent.mkdir()
            original = '{// synthetic unrelated settings\n"model":"keep", "hooks":{"Stop":[{"hooks":[{"type":"command","command":"keep"}]}]}}'
            config.write_text(original)
            install(settings, buddy, metadata.digest(buddy.read_bytes()))
            state = Document(config.read_text()).value
            self.assertEqual(state["model"], "keep")
            self.assertEqual(len(state["hooks"]["Stop"]), 2)
            install(settings, buddy, metadata.digest(buddy.read_bytes()))
            self.assertEqual(len(Document(config.read_text()).value["hooks"]["Stop"]), 2)
            self.assertFalse((project / ".codex").exists())

    def test_codebuddy_recall_only_project_never_reads_transcripts(self):
        import io
        from unittest.mock import Mock
        from agent_brain_client.runtime import project_memory
        from agent_brain_client.adapters.harnesses.codebuddy import transcript
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            settings = {"harness": "codebuddy", "enabled": True, "device_home": str(home), "project_root": str(home),
                        "capture_policy": None, "source_mode": "recall-only"}
            payload = {"hook_event_name": "UserPromptSubmit", "session_id": "native-session", "cwd": str(home), "prompt": "Synthetic current question"}
            stdin = type("Input", (), {"buffer": io.BytesIO(json.dumps(payload).encode())})()
            recall = Mock(return_value=[{"content": "Synthetic linked memory"}])
            with patch.object(project_memory, "active", return_value=True), patch.object(project_memory.sys, "stdin", stdin), \
                    patch.object(project_memory, "recalled", recall), patch.object(transcript, "observe", side_effect=AssertionError("No capture authority")), \
                    patch.object(transcript, "assistant", side_effect=AssertionError("No capture authority")):
                result = project_memory.hook_payload(settings)
            self.assertIn("Synthetic linked memory", str(result))
            recall.assert_called_once()
            self.assertFalse((home / ".codebuddy/turns").exists())

    def test_codebuddy_project_completion_resumes_from_exact_observed_marker(self):
        from unittest.mock import Mock
        from agent_brain_client.adapters.harnesses.codebuddy import transcript, project_configuration as native
        from agent_brain_client.runtime import project_memory
        from agent_brain_client.adapters.storage import metadata
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            native_file = home / ".codebuddy/projects/native-session.jsonl"
            native_file.parent.mkdir(parents=True)
            native_file.write_text("")
            payload = {"hook_event_name": "UserPromptSubmit", "session_id": "native-session", "generation_id": "generation-1",
                       "cwd": str(home), "prompt": "Synthetic prompt", "transcript_path": str(native_file)}
            settings = {"harness": "codebuddy", "device_home": str(home), "project_root": str(home), "capture_policy": "synthetic"}
            transcript.observe(home, payload, None)
            stop = dict(payload, hook_event_name="Stop", stop_hook_active=False)
            turn = native.register_completion(settings, stop, home / "settings.json", "a" * 64)
            native_file.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "Synthetic completed answer"}}) + "\n")
            task = {"schema": 1, "harness": "codebuddy-project-final", "turn_id": turn,
                    "settings": str(home / "settings.json"), "settings_sha256": "a" * 64}
            staged = Mock(return_value={"local_queue": {"saved": True, "state": "pending"}})
            with patch.object(project_memory, "load", return_value=settings), patch.object(project_memory, "active", return_value=True), \
                    patch.object(project_memory, "capture_enabled", return_value=True), patch.object(project_memory, "capture", staged), patch("agent_brain_client.runtime.delivery.schedule_project"):
                self.assertFalse(project_memory.finish_codebuddy_project(home, task))
                self.assertFalse(project_memory.finish_codebuddy_project(home, task))
            staged.assert_called_once()
            self.assertEqual(staged.call_args.args[1]["last_assistant_message"], "Synthetic completed answer")
            self.assertTrue(metadata.private_json(transcript.marker(home, payload), cache=True)["complete"])


class SharedPorts(unittest.TestCase):
    def test_common_content_runs_with_both_concrete_host_packages_forbidden(self):
        code = '''
import sys, importlib.abc, pathlib, tempfile
sys.path[:0] = [sys.argv[1], str(pathlib.Path(sys.argv[1]).parent)]
class NoHarness(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('agent_brain_client.adapters.harnesses.'):
            raise AssertionError('A shared service selected a concrete host: ' + fullname)
sys.meta_path.insert(0, NoHarness())
from agent_brain_client.adapters.storage.content_session import ContentSession
from agent_brain_client.adapters.storage.native_mapping import NativeMapping
from agent_brain_client.adapters.installation.updates import RegisteredClientInstallation
from agent_brain_client.interfaces.mcp.content import ContentServer
class Mapping(NativeMapping):
    skill_prefix = '.synthetic/skills'
    instruction_keys = frozenset()
    def _mapping(self, snapshot, owned): return {}
    def _path(self, name): raise AssertionError('Empty native projection')
class Sources:
    prefix = 'synthetic-memory:'
    def __init__(self, session): pass
    def inventory(self): return {'schema':1,'status':'complete','sources':[],'cursor':'a'*64}
class Host:
    name = 'synthetic'
    mapping = Mapping
    sources = Sources
    protected = ('.synthetic',)
    @staticmethod
    def default_home(home): return pathlib.Path(home)/'.synthetic'
    @staticmethod
    def binding(home, native): return {'device_home':str(home),'native_home':str(native),'host':'synthetic'}
class Cloud:
    grant={'scopes':['read','put'],'artifact_types':['memory','rule','skill'],'ids':{'memory':'*','rule':['synthetic-rule'],'skill':['synthetic-skill']}}
    def request(self, request): return {'request_id':request['request_id'],'result':{'content_api_version':1,'commit':'a'*40,'artifacts':[]}}
with tempfile.TemporaryDirectory() as tmp:
    home = pathlib.Path(tmp).resolve()
    policy={'schema':1,'adapter':'codex','kinds':['memory','rule','skill'],'ids':{'memory':'*','rule':['synthetic-rule'],'skill':['synthetic-skill']},'dependencies':{'commands':[],'python_modules':[]}}
    session=ContentSession(home,home/'cache',policy,Cloud(),host=Host)
    assert session.refresh()['status']=='content-refreshed'
    server=ContentServer(session)
    assert server.adapter is session
    assert not any(name.startswith('agent_brain_client.adapters.harnesses.') for name in sys.modules)
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT / "src")], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_secondary_projection_removal_keeps_pending_operations_and_is_repeatable(self):
        from agent_brain_client.adapters.installation import projection_removal
        for harness in ("codex", "codebuddy"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp).resolve()
                cloud = Cloud()
                cloud.records = {("skill", "synthetic-skill"): dict(SKILL, enabled=True)}
                session = ContentSession(home, home / "cache", POLICY, cloud, host=content_host(harness))
                session.refresh()
                before = session.mapping.owned_snapshot()
                cloud.offline = True
                request = {"request_id": "synthetic-pending-removal", "method": "content_put", "params": {
                    "kind": "memory", "id": "synthetic-topic", "expected_revision": "missing", "record": MEMORY}}
                session.invoke(request)
                with self.assertRaises(ValueError):
                    projection_removal.prepare(session)
                self.assertEqual(session.mapping.owned_snapshot(), before)
                cloud.offline = False
                session.invoke(request)
                projection_removal.remove(session)
                projection_removal.remove(session)
                self.assertFalse(session.mapping.owned_snapshot()["files"])
                self.assertEqual(session.queue_status()["confirmed"], 1)
                self.assertTrue((session.cache / "snapshots").exists())

    @patch("agent_brain_client.adapters.installation.launcher_update.prepare_native_hook")
    def test_codebuddy_hook_uninstall_and_update_preserve_explicit_pause(self, prepare):
        from types import SimpleNamespace
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        from agent_brain_client.runtime.host_maintenance import global_entry
        from agent_brain_client.adapters.storage import metadata
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            configuration.install(home, ROOT, None, surface="both")
            native = home / ".codebuddy/settings.json"
            value = configuration.Document(native.read_text()).value
            value["permissions"] = {"deny": ["Synthetic preserved permission"]}
            native.write_text(json.dumps(value))
            result = global_entry(SimpleNamespace(device_home=str(home)), harness="codebuddy", remove=True)
            self.assertEqual(result["status"], "global-hook-removed")
            configuration.refresh(home, ROOT)
            receipt = metadata.private_json(configuration.state_directory(home) / "registration.json")
            self.assertFalse(receipt["hooks_enabled"])
            checked = configuration.status(home, None, backend_check=lambda *a: (None, {"status": "synthetic-verified"}))
            self.assertTrue(checked["installed"])
            self.assertFalse(checked["collection_hooks_installed"])
            self.assertEqual(configuration.Document(native.read_text()).value["permissions"], value["permissions"])
            self.assertTrue(all(not groups for groups in configuration.Document(native.read_text()).value.get("hooks", {}).values()))
            configuration.install(home, ROOT, None, surface="both", enable_hooks=True)
            self.assertTrue(configuration.status(home, None, backend_check=lambda *a: (None, {"status": "synthetic-verified"}))["collection_hooks_installed"])

    def test_retained_codebuddy_project_pause_does_not_stop_codex(self):
        from agent_brain_client.adapters.storage.project_settings import seed_host_stops, source_active, clear_host_stop
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            project = home / "synthetic-project"
            project.mkdir()
            seed_host_stops(home, [{"harness": "codebuddy", "root": str(project), "stopped": True, "enabled": True}])
            self.assertFalse(source_active(home, project, "codebuddy"))
            self.assertTrue(source_active(home, project, "codex"))
            clear_host_stop(home, project, "codex")
            self.assertFalse(source_active(home, project, "codebuddy"))
            clear_host_stop(home, project, "codebuddy")
            self.assertTrue(source_active(home, project, "codebuddy"))

    def test_paused_codebuddy_project_never_observes_native_bodies(self):
        import io
        from agent_brain_client.runtime import project_memory
        from agent_brain_client.adapters.harnesses.codebuddy import transcript
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            p = {"harness": "codebuddy", "device_home": str(home), "project_root": str(home),
                 "capture_policy": "synthetic-paused-policy", "source_mode": "codex-project"}
            payload = {"hook_event_name": "UserPromptSubmit", "session_id": "synthetic-session", "cwd": str(home), "prompt": "Synthetic prompt"}
            stdin = type("Input", (), {"buffer": io.BytesIO(json.dumps(payload).encode())})()
            with patch.object(project_memory, "active", return_value=True), patch.object(project_memory, "capture_enabled", return_value=False), \
                    patch.object(project_memory.sys, "stdin", stdin), patch.object(project_memory, "capture", return_value={"status": "disabled"}), \
                    patch.object(project_memory, "recalled", return_value=[]), patch("agent_brain_client.runtime.delivery.schedule_project"), \
                    patch.object(transcript, "observe", side_effect=AssertionError("Paused source must not be observed")):
                project_memory.hook_payload(p, settings_path=str(home / "settings.json"), settings_sha256="a" * 64)

    def test_multi_host_reinstall_keeps_codex_stop_out_of_codebuddy_scope(self):
        from agent_brain_client.adapters.storage.project_settings import retained_controls, seed_host_stops, source_active
        from agent_brain_client.adapters.storage import metadata
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            project = home / "synthetic-project"
            project.mkdir()
            directory = home / ".config/agent-brain-client/project-memory/synthetic-control"
            private_directory(directory)
            off = directory / "memory.off"
            off.touch(mode=0o600)
            metadata.write(directory / "settings.json", {"schema": 1, "harness": "codex", "enabled": True,
                "device_home": str(home), "project_root": str(project), "off_file": str(off), "source_mode": "codex-project"})
            controls = retained_controls(home, [], isolate_hosts=True)
            self.assertEqual(controls["blocked_roots"], [])
            seed_host_stops(home, controls["states"], include_legacy=controls["host_scoped"])
            self.assertFalse(source_active(home, project, "codex"))
            self.assertTrue(source_active(home, project, "codebuddy"))

    @unittest.skipUnless(sys.platform == "darwin", "Native Mac installation contract")
    def test_common_install_and_source_runtime_do_not_require_any_concrete_host(self):
        code = '''
import sys, pathlib, importlib.abc
sys.path[:0] = [sys.argv[1], sys.argv[2], str(pathlib.Path(sys.argv[1]).parent)]
import test_mac_first_install as fixture
f=fixture.FirstInstall('test_first_install_prepared_environment_and_exact_native_transaction')
f.setUp()
try:
    f.fixture()
    f.harness='codebuddy'
    f.native=f.home/'.config/agent-brain-client/shared-native'
    f.native.mkdir()
    f.config=f.native/'config.toml'
    for name in list(sys.modules):
        if name.startswith('agent_brain_client.adapters.harnesses.'):
            del sys.modules[name]
    class NoHarness(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith('agent_brain_client.adapters.harnesses.'):
                raise AssertionError('Shared install required native implementation: '+fullname)
    sys.meta_path.insert(0,NoHarness())
    result=f.run_wizard()
    assert result['installed'],result
    from types import SimpleNamespace
    from agent_brain_client.runtime.host_maintenance import install_sources
    prepared=install_sources(SimpleNamespace(device_home=str(f.home),profile_id=None,operator=None,operator_sha256=None))
    assert prepared['harness']=='codebuddy',prepared
    from agent_brain_client.runtime.host_binding import load_core
    load_core(f.home)
    from agent_brain_client.runtime.updates import reexec_update_runtime
    reexec_update_runtime(f.home,'check-update',runner=lambda *a,**k:SimpleNamespace(returncode=0))
    assert not any(name.startswith('agent_brain_client.adapters.harnesses.') for name in sys.modules)
    assert not (f.home/'.codex').exists()
    assert not (f.home/'.agents').exists()
finally:
    f.doCleanups()
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT / "src"), str(ROOT / "tests")], capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_fresh_reinstall_retains_both_mapping_ledgers_and_codebuddy_pause(self):
        from test_fresh_installation import FreshInstallation
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        from agent_brain_client.adapters.storage import metadata
        f = FreshInstallation("test_retains_old_files_and_queue_restores_only_memory_projection")
        f.setUp()
        self.addCleanup(f.doCleanups)
        configuration.install(f.home, ROOT, None, surface="cli", enable_hooks=False)
        cloud = Cloud()
        cloud.grant = f.grant
        cloud.records = {("memory", "personal-owner"): copy.deepcopy(MEMORY)}
        buddy_cache = f.root / "codebuddy/content-cache"
        session = ContentSession(f.home, buddy_cache, f.content, cloud, host=content_host("codebuddy"))
        self.assertEqual(session.refresh()["status"], "content-refreshed")
        original = session.mapping.owned_snapshot()
        old_queue = buddy_cache / "outbox/retained-original-request.json"
        old_queue.parent.mkdir(exist_ok=True)
        old_queue.write_bytes(b"SYNTHETIC_OLD_QUEUE_NOT_REPLAYED")
        old_queue.chmod(0o600)
        result = f.prepare()
        self.assertEqual(result["status"], "fresh-install-ready")
        record = f.port.load()
        self.assertEqual(record["mapping"]["additional_projections"]["codebuddy"]["mapping"]["owned"], original)
        self.assertEqual(record["mapping"]["registered_surfaces"]["codebuddy"], {"surface": "cli", "hooks_enabled": False})
        from agent_brain_client.runtime.fresh_installation import retained_installation
        switched = retained_installation(f.home, Path(record["native"]), target_host=content_host("codebuddy"))
        switched.restore_mapping(buddy_cache, f.content)
        codex_cache = f.root / "codex/content-cache"
        if sys.platform == "win32":
            from agent_brain_client.adapters.platforms.filesystem import private_mode
            private_mode(codex_cache.parent, directory=True, owner=True)
        self.assertEqual(metadata.private_json(codex_cache / "owned.json", cache=True), record["mapping"]["owned"])
        self.assertEqual(metadata.private_json(buddy_cache / "owned.json", cache=True), original)
        self.assertFalse(old_queue.exists())
        self.assertEqual((Path(record["archive"]) / "codebuddy/content-cache/outbox/retained-original-request.json").read_bytes(), b"SYNTHETIC_OLD_QUEUE_NOT_REPLAYED")
        self.assertEqual(f.memory_path.read_text(), "Keep the existing remembered fact.")

    @unittest.skipUnless(sys.platform == "darwin", "Native Mac installation contract")
    @patch("agent_brain_client.adapters.installation.launcher_update.prepare_native_hook")
    def test_public_uninstall_checks_secondary_queue_then_removes_both_owned_projections(self, prepare):
        from test_client_uninstall import ClientUninstall
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        from agent_brain_client.runtime import client_lifecycle
        from agent_brain_client.adapters.storage import metadata
        f = ClientUninstall("test_cancel_is_read_only_and_status_never_reads_credentials")
        f.setUp()
        self.addCleanup(f.doCleanups)
        configuration.install(f.home, ROOT, None, surface="cli")
        cache = f.state / "codebuddy/content-cache"
        session = ContentSession(f.home, cache, f.adapter.policy, f.cloud, host=content_host("codebuddy"))
        session.refresh()
        f.cloud.offline = True
        request = {"request_id": "synthetic-secondary-pending", "method": "content_put", "params": {
            "kind": "memory", "id": "synthetic-memory", "expected_revision": contract.envelope("memory", "synthetic-memory", MEMORY)["revision"],
            "record": dict(MEMORY, body="Synthetic revised preference")}}
        session.invoke(request)
        config_before = f.f.config.read_bytes()
        result = client_lifecycle.uninstall(f.home, lambda message: "Continue")
        self.assertEqual((result["status"], result["harness"]), ("uninstall-unconfirmed-writes", "codebuddy"))
        self.assertEqual(f.f.config.read_bytes(), config_before)
        f.cloud.offline = False
        session.invoke(request)
        result = client_lifecycle.uninstall(f.home, lambda message: "Continue")
        self.assertEqual(result["status"], "client-uninstalled")
        self.assertFalse(session.mapping.owned_snapshot()["files"])
        self.assertTrue(metadata.private_json(configuration.state_directory(f.home) / "registration.json")["removed"])
        self.assertEqual(session.queue_status()["confirmed"], 1)
        self.assertEqual(client_lifecycle.uninstall(f.home, lambda message: "Continue")["status"], "client-uninstalled")

    def test_incomplete_manual_queue_sync_returns_nonzero(self):
        import io
        from agent_brain_client.interfaces.cli import collection as cli
        from agent_brain_client.runtime import collection
        output = io.StringIO()
        with patch.object(collection, "load", return_value={}), patch.object(collection, "flush", return_value={"status": "global-queue-sync-incomplete"}), \
                patch.object(cli.sys, "stdout", output):
            self.assertEqual(cli.main(["global-sync"]), 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "global-queue-sync-incomplete")

    @unittest.skipUnless(sys.platform == "darwin", "Native Mac installation contract")
    @patch("agent_brain_client.adapters.installation.launcher_update.prepare_native_hook")
    def test_device_uninstall_can_resume_a_legacy_projection_only_receipt(self, prepare):
        from test_client_uninstall import ClientUninstall
        from agent_brain_client.adapters.installation import projection_removal
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        from agent_brain_client.runtime import client_lifecycle

        f = ClientUninstall("test_cancel_is_read_only_and_status_never_reads_credentials")
        f.setUp()
        self.addCleanup(f.doCleanups)
        configuration.install(f.home, ROOT, None, surface="cli")
        cache = f.state / "codebuddy/content-cache"
        session = ContentSession(f.home, cache, f.adapter.policy, f.cloud, host=content_host("codebuddy"))
        self.assertEqual(session.refresh()["status"], "content-refreshed")
        skill = f.home / ".codebuddy/skills/synthetic-skill/SKILL.md"
        original_unlink = Path.unlink

        def interrupt_projection(path, *args, **options):
            original_unlink(path, *args, **options)
            if path == skill:
                raise SystemExit("Synthetic interruption during legacy projection removal")

        with patch.object(Path, "unlink", interrupt_projection), self.assertRaises(SystemExit):
            projection_removal.remove(session)
        self.assertFalse(client_lifecycle.journal_path(f.home).exists())
        self.assertFalse(skill.exists())
        self.assertEqual(client_lifecycle.uninstall(f.home, lambda message: "Cancel")["status"], "uninstall-cancelled")
        self.assertFalse(skill.exists())
        self.assertFalse(client_lifecycle.journal_path(f.home).exists())
        self.assertEqual(client_lifecycle.uninstall(f.home, lambda message: "Continue")["status"], "client-uninstalled")
        self.assertFalse(skill.exists())
        self.assertFalse(session.mapping.owned_snapshot()["files"])
        self.assertEqual(f.f.backend.record, f.identity)


@unittest.skipUnless(sys.platform == "darwin", "Native Mac installation contract")
class IndependentHostControl(unittest.TestCase):
    def setUp(self):
        prepare = patch("agent_brain_client.adapters.installation.launcher_update.prepare_native_hook")
        prepare.start()
        self.addCleanup(prepare.stop)

    def shared_install(self, harness):
        from test_mac_first_install import FirstInstall
        f = FirstInstall("test_first_install_prepared_environment_and_exact_native_transaction")
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.fixture()
        f.harness = harness
        f.native = f.home / ".config/agent-brain-client/shared-native"
        private_directory(f.native)
        f.config = f.native / "config.toml"
        self.assertTrue(f.run_wizard()["installed"])
        return f

    def test_both_install_orders_keep_native_pause_and_removal_out_of_shared_control(self):
        from types import SimpleNamespace
        from agent_brain_client.adapters.harnesses.codex import binding
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        from agent_brain_client.adapters.storage.registration import registration
        from agent_brain_client.runtime.host_maintenance import install_sources
        from agent_brain_client.runtime.host_binding import backend
        from agent_brain_client.interfaces.cli.install import selected_native
        for first in ("codex", "codebuddy"):
            with self.subTest(first=first):
                f = self.shared_install(first)
                native = f.home / "selected-codex-profile"
                if first == "codex":
                    binding.install(f.home, native=native)
                install_sources(SimpleNamespace(device_home=str(f.home), profile_id=None,
                                                 operator=None, operator_sha256=None, harness=first))
                configuration.install(f.home, ROOT, None, surface="cli")
                if first == "codebuddy":
                    self.assertFalse(native.exists())
                    binding.install(f.home, native=native)
                    install_sources(SimpleNamespace(device_home=str(f.home), profile_id=None,
                                                     operator=None, operator_sha256=None, harness="codex"))
                _, policy = registration(f.home)
                runtime = policy["runtime"]
                codex_native, codex_cache = content_host("codex").location(f.home, runtime)
                buddy_native, buddy_cache = content_host("codebuddy").location(f.home, runtime)
                self.assertEqual(codex_native, native)
                self.assertEqual(buddy_native, f.home / ".codebuddy")
                self.assertNotEqual(codex_cache, buddy_cache)
                self.assertEqual(selected_native(f.home, "codex"), f.native)
                self.assertEqual(selected_native(f.home, "codebuddy"), f.native)
                project = f.home / "synthetic-project"
                project.mkdir()
                from agent_brain_client.runtime import project_memory
                from agent_brain_client.adapters.storage.metadata import private_json
                for harness in ("codex", "codebuddy"):
                    prepared = project_memory.setup(SimpleNamespace(device_home=str(f.home), project_root=str(project),
                        output=str(f.home / ".config/agent-brain-client/project-memory" / harness / "settings.json"),
                        harness=harness, query="Synthetic", server_bundle=None))
                    settings = project_memory.load(prepared["settings"], prepared["settings_sha256"])
                    self.assertEqual(settings["codex_home"], str(native if harness == "codex" else buddy_native))
                    self.assertEqual(private_json(prepared["settings"])["harness"], harness)
                shared_before = f.config.read_bytes()
                config = native / "config.toml"
                config.write_bytes(config.read_bytes().replace(b"enabled = true", b"enabled = false"))
                with patch("agent_brain_client.adapters.packages.client.ClientReleases.verified_active", return_value={"version": "synthetic"}):
                    self.assertTrue(backend(f.home, None)[1]["installed"])
                self.assertFalse(binding.status(f.home, runtime)["service_enabled"])
                binding.install(f.home)
                self.assertFalse(binding.status(f.home, runtime)["service_enabled"])
                self.assertEqual(f.config.read_bytes(), shared_before)
                # A removed/edited native profile is a host repair issue. The
                # other host still authenticates against private shared control.
                config.unlink()
                with patch("agent_brain_client.adapters.packages.client.ClientReleases.verified_active", return_value={"version": "synthetic"}):
                    self.assertTrue(backend(f.home, None)[1]["installed"])
                self.assertEqual(f.config.read_bytes(), shared_before)
                self.assertFalse((f.home / ".codex").exists())

    def test_early_hook_recovery_uses_native_profile_and_authenticates_shared_control(self):
        from types import SimpleNamespace
        from agent_brain_client.adapters.harnesses.codex import binding, collection_installation as hooks
        from agent_brain_client.adapters.storage import host_files
        from agent_brain_client.adapters.storage.collection import settings_path
        from agent_brain_client.runtime.host_maintenance import install_sources
        from agent_brain_client.runtime.installation_completion import resume_setup
        f = self.shared_install("codex")
        native = f.home / "selected-codex-profile"
        binding.install(f.home, native=native)
        install_sources(SimpleNamespace(device_home=str(f.home), profile_id=None,
                                         operator=None, operator_sha256=None, harness="codex"))
        shared_before = f.config.read_bytes()
        native_file = native / "config.toml"
        before = native_file.read_bytes()
        original_apply = host_files.host_apply
        def interrupt(plan, save):
            original_apply(plan, save)
            raise SystemExit("Synthetic interruption after native commit")
        owned = hooks.split_owned(before)[1]
        wanted = owned.replace("timeout = 120", "timeout = 121")
        self.assertNotEqual(owned, wanted)
        with patch.object(host_files, "host_apply", side_effect=interrupt), self.assertRaises(SystemExit):
            hooks.transact(native, settings_path(f.home).parent, wanted)
        self.assertNotEqual(native_file.read_bytes(), before)
        self.assertIsNone(resume_setup(f.home, f.native))
        self.assertEqual(native_file.read_bytes(), before)
        self.assertEqual(f.config.read_bytes(), shared_before)
        self.assertEqual(hooks.read_receipt(settings_path(f.home).parent / "hook-install.json")["phase"], "rolled-back")

    def test_private_control_uninstall_detaches_both_native_entries_and_retains_data(self):
        from types import SimpleNamespace
        from agent_brain_client.adapters.harnesses.codex import binding
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        from agent_brain_client.adapters.storage.registration import registration
        from agent_brain_client.runtime.host_maintenance import install_sources
        from agent_brain_client.runtime.client_lifecycle import uninstall
        f = self.shared_install("codebuddy")
        native = f.home / "selected-codex-profile"
        binding.install(f.home, native=native)
        install_sources(SimpleNamespace(device_home=str(f.home), profile_id=None,
                                         operator=None, operator_sha256=None, harness="codex"))
        configuration.install(f.home, ROOT, None, surface="cli")
        _, policy = registration(f.home)
        cloud = Cloud()
        cloud.grant = copy.deepcopy(f.fixture_grant)
        from agent_brain_client.adapters.storage.metadata import private_json
        content = private_json(policy["runtime"]["content_policy"], policy["runtime"]["content_policy_sha256"])
        cloud.records = {("memory", "synthetic-memory"): MEMORY}
        sessions = []
        for harness in ("codex", "codebuddy"):
            port = content_host(harness)
            actual, cache = port.location(f.home, policy["runtime"])
            session = ContentSession(f.home, cache, content, cloud, host=port, native_home=actual)
            self.assertEqual(session.refresh()["status"], "content-refreshed")
            sessions.append(session)
        config = native / "config.toml"
        config.write_bytes(config.read_bytes().replace(b"enabled = true", b"enabled = false"))
        with patch("agent_brain_client.adapters.packages.client.ClientReleases.verified_active", return_value={"version": "synthetic"}):
            result = uninstall(f.home, lambda message: "Continue")
        self.assertEqual(result["status"], "client-uninstalled", result)
        self.assertNotIn(b"agent_brain_server", (native / "config.toml").read_bytes())
        self.assertNotIn(b"Agent Brain global memory", (native / "config.toml").read_bytes())
        self.assertTrue(binding.receipt(f.home)["removed"])
        for session in sessions:
            self.assertFalse(session.mapping.owned_snapshot()["files"])
            self.assertTrue(session.cache.is_dir())
        self.assertTrue(f.run_wizard()["installed"])
        from agent_brain_client.runtime.installation_completion import complete
        with patch("agent_brain_client.runtime.installation_completion.source_setup", return_value={"collection_enabled": True}):
            result = complete(f.home, {"installed": True}, harness="codex",
                              refresh=lambda *args: {"status": "content-current"})
        self.assertTrue(result["local_setup_complete"], result)
        self.assertFalse(binding.receipt(f.home)["removed"])
        self.assertFalse(binding.status(f.home, policy["runtime"])["service_enabled"])
        self.assertIn(b"Agent Brain global memory", (native / "config.toml").read_bytes())

    def test_interrupted_second_native_removal_resumes_device_uninstall_through_repair(self):
        from agent_brain_client.adapters.harnesses.codex import binding
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        from agent_brain_client.adapters.storage import metadata
        from agent_brain_client.runtime import client_lifecycle

        f = self.shared_install("codebuddy")
        native = f.home / "selected-codex-profile"
        private_directory(native)
        codex_config = native / "config.toml"
        codex_config.write_text('model = "synthetic-user-model"\n')
        binding.install(f.home, native=native)
        configuration.install(f.home, ROOT, None, surface="cli")
        codebuddy_state = configuration.state_directory(f.home)
        identity = copy.deepcopy(f.backend.record)
        original_write = metadata.write
        original_unlink = Path.unlink
        registration = f.home / ".config/agent-brain-client/codex-mcp.json"

        def crash_on_host_completion(path, record, **options):
            if path == codebuddy_state / "transaction.json" and record["phase"] == "complete":
                raise SystemExit("Synthetic interruption before CodeBuddy transaction completion")
            return original_write(path, record, **options)

        def crash_after_shared_registration(path, *args, **options):
            original_unlink(path, *args, **options)
            if path == registration:
                raise SystemExit("Synthetic interruption after removing shared registration")

        with patch("agent_brain_client.adapters.packages.client.ClientReleases.verified_active",
                   return_value={"version": "synthetic"}):
            with patch.object(metadata, "write", side_effect=crash_on_host_completion), self.assertRaises(SystemExit):
                client_lifecycle.uninstall(f.home, lambda message: "Continue")
            self.assertTrue(binding.receipt(f.home)["removed"])
            self.assertTrue(metadata.private_json(codebuddy_state / "registration.json")["removed"])
            self.assertEqual(client_lifecycle.status(f.home)["status"], "uninstall-resume-required")
            with patch.object(Path, "unlink", crash_after_shared_registration), self.assertRaises(SystemExit):
                client_lifecycle.repair(f.home)
            self.assertFalse(registration.exists())
            self.assertEqual(client_lifecycle.status(f.home)["status"], "uninstall-resume-required")
            result = client_lifecycle.uninstall(f.home, lambda message: self.fail("Repeated confirmation"))
            self.assertEqual(result["status"], "client-uninstalled")
            self.assertTrue(client_lifecycle.status(f.home)["uninstall_verified"])

        self.assertEqual(metadata.private_json(codebuddy_state / "transaction.json")["phase"], "complete")
        self.assertEqual(codex_config.read_text().strip(), 'model = "synthetic-user-model"')
        self.assertEqual(f.backend.record, identity)

    def test_unbind_leaves_other_host_shared_control_and_original_outbox_intact(self):
        from types import SimpleNamespace
        from agent_brain_client.adapters.harnesses.codex import binding, collection_installation
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        from agent_brain_client.adapters.storage.collection import settings_path
        from agent_brain_client.adapters.storage.registration import registration
        from agent_brain_client.adapters.storage.metadata import private_json
        from agent_brain_client.runtime import client_lifecycle
        from agent_brain_client.runtime.host_maintenance import install_sources

        f = self.shared_install("codebuddy")
        native = f.home / "selected-codex-profile"
        private_directory(native)
        config = native / "config.toml"
        config.write_text('model = "synthetic-user-model"\n')
        binding.install(f.home, native=native)
        install_sources(SimpleNamespace(device_home=str(f.home), profile_id=None,
                                         operator=None, operator_sha256=None, harness="codex"))
        configuration.install(f.home, ROOT, None, surface="cli")
        meta, policy = registration(f.home)
        runtime = policy["runtime"]
        port = content_host("codex")
        actual, cache = port.location(f.home, runtime)
        cloud = Cloud()
        cloud.grant = copy.deepcopy(f.fixture_grant)
        cloud.offline = True
        content = private_json(runtime["content_policy"], runtime["content_policy_sha256"])
        session = ContentSession(f.home, cache, content, cloud, host=port, native_home=actual)
        request = {"request_id": "synthetic-unbind-pending", "method": "content_put", "params": {
            "kind": "memory", "id": "synthetic-memory", "expected_revision": "missing", "record": MEMORY}}
        session.invoke(request)
        outbox = cache / "outbox/synthetic-unbind-pending.json"
        buddy_receipt = configuration.state_directory(f.home) / "registration.json"
        buddy_native = [Path(path) for path in private_json(buddy_receipt)["mcp_paths"]]
        retained = {*buddy_native, buddy_receipt, f.config, settings_path(f.home), outbox}
        before = {path: path.read_bytes() for path in retained}
        identity = copy.deepcopy(f.backend.record)

        result = client_lifecycle.unbind(f.home, harness="codex")
        self.assertEqual((result["status"], result["native_changed"]), ("host-unbound", True))
        self.assertEqual(config.read_text().strip(), 'model = "synthetic-user-model"')
        self.assertIsNone(collection_installation.split_owned(config.read_bytes())[1])
        self.assertFalse(client_lifecycle.unbind(f.home, harness="codex")["native_changed"])
        self.assertEqual(registration(f.home)[0], meta)
        self.assertEqual({path: path.read_bytes() for path in retained}, before)
        self.assertFalse(client_lifecycle.journal_path(f.home).exists())
        self.assertEqual(f.backend.record, identity)

        with patch("agent_brain_client.adapters.packages.client.ClientReleases.verified_active",
                   return_value={"version": "synthetic"}):
            result = client_lifecycle.uninstall(f.home, lambda message: self.fail("Unconfirmed queue"))
            self.assertEqual((result["status"], result["harness"]), ("uninstall-unconfirmed-writes", "codex"))
            self.assertEqual({path: path.read_bytes() for path in retained}, before)
            self.assertFalse(client_lifecycle.journal_path(f.home).exists())
            cloud.offline = False
            session.invoke(request)
            queue_bytes = outbox.read_bytes()
            result = client_lifecycle.uninstall(f.home, lambda message: "Continue")
        self.assertEqual(result["status"], "client-uninstalled")
        self.assertEqual(outbox.read_bytes(), queue_bytes)
        self.assertEqual(f.backend.record, identity)

    def test_codebuddy_unbind_preserves_codex_and_unrelated_codebuddy_config(self):
        from agent_brain_client.adapters.harnesses.codex import binding
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        from agent_brain_client.runtime import client_lifecycle

        f = self.shared_install("codebuddy")
        native = f.home / "selected-codex-profile"
        binding.install(f.home, native=native)
        buddy_home = f.home / ".codebuddy"
        private_directory(buddy_home)
        buddy_mcp = buddy_home / ".mcp.json"
        buddy_mcp.write_text('{"mcpServers": {"other": {"command": "synthetic-user-server"}}}\n')
        buddy_settings = buddy_home / "settings.json"
        buddy_settings.write_text('{"userSetting": true}\n')
        configuration.install(f.home, ROOT, None, surface="cli")
        retained = (f.config, native / "config.toml", binding.directory(f.home) / "registration.json")
        before = [path.read_bytes() for path in retained]

        result = client_lifecycle.unbind(f.home, harness="codebuddy")
        self.assertEqual((result["status"], result["native_changed"]), ("host-unbound", True))
        self.assertEqual([path.read_bytes() for path in retained], before)
        self.assertEqual(json.loads(buddy_mcp.read_text())["mcpServers"], {"other": {"command": "synthetic-user-server"}})
        self.assertTrue(json.loads(buddy_settings.read_text())["userSetting"])
        self.assertFalse(client_lifecycle.unbind(f.home, harness="codebuddy")["native_changed"])
        self.assertFalse(client_lifecycle.journal_path(f.home).exists())


class NativeBindingTransaction(unittest.TestCase):
    def test_native_codex_transaction_preserves_pause_and_unrelated_toml(self):
        from agent_brain_client.adapters.harnesses.codex import binding
        from agent_brain_client.adapters.storage import registration
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            native = home / "native-profile"
            private_directory(native)
            file = native / "config.toml"
            file.write_text('model = "synthetic"\n[mcp_servers.other]\ncommand = "preserve"\n')
            file.chmod(0o600)
            policy = {"runtime": {"codex_home": str(home / ".config/agent-brain-client/shared-native")}}
            from agent_brain_client.adapters.storage import metadata
            policy_file = home / ".config/agent-brain-client/policy.json"
            private_directory(policy_file.parent)
            metadata.write(policy_file, policy)
            metadata.write(policy_file.parent / "codex-mcp.json", {"release_policy": str(policy_file),
                "release_policy_sha256": metadata.digest(policy_file.read_bytes())})
            command = {"command": str(Path(sys.executable).resolve()), "args": ["--harness", "codebuddy"],
                       "env": {}, "enabled": True}
            with patch.object(registration, "registration", return_value=({}, policy)), patch(
                "agent_brain_client.adapters.installation.installation.registered_context", return_value=(policy, command, None, None)):
                binding.install(home, native=native)
                raw = file.read_bytes().replace(b"enabled = true", b"enabled = false")
                file.write_bytes(raw)
                binding.install(home)
                self.assertIn(b"enabled = false", file.read_bytes())
                self.assertIn(b'command = "preserve"', file.read_bytes())
                self.assertIn(b'model = "synthetic"', file.read_bytes())
                file.write_bytes(file.read_bytes().replace(b"enabled = false", b"enabled = true"))
                resumed = file.read_bytes()
                self.assertFalse(binding.install(home)["native_changed"])
                self.assertEqual(file.read_bytes(), resumed)
                self.assertTrue(binding.status(home, policy["runtime"])["service_enabled"])
                binding.install(home, remove=True)
                self.assertNotIn(b"agent_brain_server", file.read_bytes())
                self.assertIn(b'command = "preserve"', file.read_bytes())

    def test_update_exit_requires_verified_registration_when_binding_recovery_runs(self):
        from agent_brain_client.interfaces.cli.install import lifecycle_exit
        update = {"status": "client-update-current", "channel_checked": True}
        self.assertEqual(lifecycle_exit("update", update), 0)
        recovery = dict(update, installation_stages={
            "mcp_registration": "unverified",
            "setup": {"stage": "registration", "status": "pending-review-or-resume"},
        })
        self.assertEqual(lifecycle_exit("update", recovery), 2)

    def test_native_collision_is_rejected_before_shared_enrollment(self):
        import os
        from agent_brain_client.interfaces.cli.install import selected_native, lifecycle_exit
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            native = home / "native-profile"
            private_directory(native)
            file = native / "config.toml"
            original = b'[mcp_servers.agent_brain_server]\ncommand = "preserve-legacy-service"\n'
            file.write_bytes(original)
            file.chmod(0o600)
            with patch.dict(os.environ, CODEX_HOME=str(native)):
                with self.assertRaises(ValueError):
                    selected_native(home, "codex")
                self.assertEqual(selected_native(home, "codebuddy"), home / ".config/agent-brain-client/shared-native")
            self.assertEqual(file.read_bytes(), original)
            self.assertFalse((home / ".config/agent-brain-client").exists())
        self.assertEqual(lifecycle_exit("install", {"installed": True,
            "installation_stages": {"setup": {"stage": "native-mcp", "status": "pending-review-or-resume"}}}), 2)
