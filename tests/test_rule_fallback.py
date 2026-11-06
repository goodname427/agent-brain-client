"""Synthetic Rule fallback: no real Harness, native config or remote service."""

from contextlib import nullcontext
import copy
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agent_brain_client.domain import contract
from agent_brain_client.domain.errors import CacheBlocked
from agent_brain_client.domain.hook_coverage import CAPABILITIES, fallback_plan
from agent_brain_client.application.memory.rule_fallback import (
    RuleFallback,
    rule_instructions,
)
from agent_brain_client.adapters.storage.content import FileContentOutbox
from test_harness_core import MemoryHost
from test_content_outbox import Cloud, POLICY
from agent_brain_client.adapters.platforms.filesystem import cache_mode

ROOT = Path(__file__).resolve().parents[1]


class RuleReceipts:
    def _validate_target(self, value, kind, identity):
        if value != contract.envelope(kind, identity, value["record"]):
            raise CacheBlocked()

    def _read_target(self, kind, identity):
        return self.client.request(
            {
                "request_id": "synthetic_conflict_read",
                "method": "content_read",
                "params": {"kind": kind, "id": identity},
            }
        )["result"]


class RuleHost(RuleReceipts, MemoryHost):
    _outbox_locked = staticmethod(lambda *args: nullcontext())

    def __init__(self, root, client, rows=None):
        super().__init__(root, rows)
        self.client = client

    def methods(self):
        return {"content_put", "memory_query"}


class DiskRuleHost(RuleReceipts, FileContentOutbox):
    def __init__(self, home, cache, client):
        super().__init__(
            home,
            cache,
            POLICY,
            client,
            binding={"host": "fictional-rule-fixture", "schema": 1},
        )


class RuleFallbackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cloud = Cloud()
        self.host = RuleHost(self.root, self.cloud)
        self.hooks = dict.fromkeys(CAPABILITIES, False)
        self.enabled = True
        self.approved = True
        self.context = {"origin": None, "session_id": None}
        self.core = self.restart()

    def restart(self):
        return RuleFallback(
            self.host,
            hooks=lambda: dict(self.hooks),
            enabled=lambda: self.enabled,
            source_gate=lambda *args: self.approved,
        )

    def capture(self, **kwargs):
        return self.core.capture(
            "assistant_final",
            "local-submission-1",
            "Synthetic Agent summary",
            representation="agent-summary",
            source_context=self.context,
            **kwargs,
        )

    def test_all_eight_hook_combinations_have_one_owner_and_only_missing_instructions(
        self,
    ):
        for values in itertools.product((False, True), repeat=3):
            hooks = dict(zip(CAPABILITIES, values))
            self.assertEqual(
                fallback_plan(hooks),
                {
                    name: "hook" if hooks[name] else "unavailable"
                    for name in CAPABILITIES
                },
            )
            self.assertEqual(rule_instructions(hooks), [])
            self.assertEqual(
                fallback_plan(hooks, rule_enabled=True),
                {name: "hook" if hooks[name] else "rule" for name in CAPABILITIES},
            )
            guidance = rule_instructions(hooks, rule_enabled=True)
            self.assertEqual(
                [entry["capability"] for entry in guidance],
                [name for name in CAPABILITIES if not hooks[name]],
            )
        for bad in ({}, {"session_start": True}, dict.fromkeys(CAPABILITIES, "yes")):
            with self.assertRaises(ValueError):
                fallback_plan(bad)
        with self.assertRaises(ValueError):
            fallback_plan(self.hooks, rule_enabled=1)

    def test_shared_import_is_inert_and_exact_inventory_has_no_host_wiring(self):
        code = """import sys
sys.path.insert(0,sys.argv[1])
before=list(sys.path)
from agent_brain_client.domain import hook_coverage
from agent_brain_client.application.memory import rule_fallback
assert sys.path==before
assert not any(n.startswith(('agent_brain_client.adapters','agent_brain_client.runtime','agent_brain_client.interfaces')) for n in sys.modules)
"""
        run = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, str(ROOT / "src")],
            capture_output=True,
        )
        self.assertEqual(run.returncode, 0, run.stderr.decode())
        from agent_brain_client.adapters.packages.layout import PYTHON_FILES

        for name in ("domain/hook_coverage.py", "application/memory/rule_fallback.py"):
            self.assertIn("src/agent_brain_client/" + name, PYTHON_FILES)
        for owner in ("adapters/harnesses", "interfaces", "runtime"):
            for path in (ROOT / "src/agent_brain_client" / owner).rglob("*.py"):
                self.assertNotIn("rule_fallback", path.read_text(encoding="utf-8"))

    def test_manual_summary_is_not_a_native_event_and_null_identity_remains_null(self):
        result = self.capture()
        self.assertTrue(result["local_queue"]["server_saved"])
        request = self.cloud.calls[0]
        self.assertEqual(request["method"], "content_put")
        self.assertEqual(request["params"]["source_context"], self.context)
        contract.validate(request["method"], request["params"])
        body = json.loads(request["params"]["record"]["body"])
        self.assertEqual(body["representation"], "agent-summary")
        self.assertEqual(body["source"], "rule")
        self.assertEqual(body["operation_id"], "local-submission-1")
        self.assertNotIn("messages", body)
        self.assertNotIn("session_id", body)
        self.assertFalse(any(name.startswith("hook-turns/") for name in self.host.rows))

    def test_capture_use_case_needs_only_the_typed_queue_port(self):
        class QueuePort:
            queue_sha = "a" * 64

            def submit_rule_capture(self, request, *, gate):
                self.request = request
                self.allowed = gate()
                return {"status": "synthetic-port-result"}

        port = QueuePort()
        core = RuleFallback(
            port,
            hooks=lambda: self.hooks,
            enabled=lambda: True,
            source_gate=lambda *args: True,
        )
        result = core.capture(
            "assistant_final",
            "port-operation",
            "Synthetic summary",
            representation="agent-summary",
            source_context=self.context,
        )
        self.assertEqual(result, {"status": "synthetic-port-result"})
        self.assertTrue(port.allowed)
        self.assertEqual(port.request["method"], "content_put")
        self.assertEqual(port.request["params"]["source_context"], self.context)

    def test_observation_roles_are_explicit_and_never_coerced_from_summary(self):
        for capability, representation in [
            ("user_prompt", "observed-user-message"),
            ("assistant_final", "observed-assistant-message"),
        ]:
            result = self.core.capture(
                capability,
                capability,
                "Synthetic observed text",
                representation=representation,
                source_context=self.context,
            )
            body = json.loads(result["result"]["record"]["body"])
            self.assertEqual(body["representation"], representation)
        for capability, representation in [
            ("user_prompt", "observed-assistant-message"),
            ("assistant_final", "final"),
            ("session_start", "agent-summary"),
        ]:
            with self.assertRaises(ValueError):
                self.core.capture(
                    capability,
                    "local-operation",
                    "Synthetic text",
                    representation=representation,
                    source_context=self.context,
                )

    def test_offline_and_lost_ack_restart_replay_exact_bytes_once(self):
        self.cloud.offline = True
        result = self.capture()
        self.assertEqual(result["local_queue"]["state"], "pending")
        self.assertFalse(result["persistence"]["durable"])
        self.assertTrue(result["local_queue"]["saved"])
        original = copy.deepcopy(self.cloud.calls[-1])
        saved = copy.deepcopy(self.host.rows)
        self.host = RuleHost(self.root, self.cloud, self.host.rows)
        self.core = self.restart()
        # Generic refresh/drain cannot replay Rule text or any other request.
        self.cloud.offline = False
        self.host._drain()
        self.assertEqual(self.cloud.puts, 0)
        self.assertEqual(self.host.rows, saved)
        self.cloud.lose_ack = True
        self.assertEqual(self.capture()["local_queue"]["state"], "pending")
        self.assertEqual(self.cloud.puts, 1)
        self.host = RuleHost(self.root, self.cloud, self.host.rows)
        self.core = self.restart()
        self.assertEqual(self.capture()["local_queue"]["state"], "confirmed")
        self.assertEqual(self.cloud.puts, 1)
        self.assertEqual(self.cloud.calls, [original, original, original])
        self.capture()
        self.assertEqual(len(self.cloud.calls), 3)

    def test_changed_payload_refused_and_unrelated_pending_request_not_drained(self):
        self.cloud.offline = True
        self.capture()
        saved = copy.deepcopy(self.host.rows)
        with self.assertRaises(CacheBlocked):
            self.core.capture(
                "assistant_final",
                "local-submission-1",
                "Changed summary",
                representation="agent-summary",
                source_context=self.context,
            )
        self.assertEqual(self.host.rows, saved)
        request = copy.deepcopy(self.cloud.calls[0])
        request["request_id"] = "synthetic_unrelated_write"
        request["params"]["id"] = "unrelated-memory"
        self.host._enqueue(request)
        self.cloud.offline = False
        self.capture()
        self.assertEqual(self.cloud.puts, 1)
        self.assertEqual(self.host.queue_status()["pending"], 1)
        self.assertTrue(
            all(call["params"]["id"] != "unrelated-memory" for call in self.cloud.calls)
        )

    def test_current_hook_enable_source_and_grant_checks_cannot_be_bypassed_on_retry(
        self,
    ):
        self.cloud.offline = True
        self.capture()
        calls = len(self.cloud.calls)
        self.cloud.offline = False
        for attribute, value in [("enabled", False), ("approved", False)]:
            setattr(self, attribute, value)
            self.assertEqual(self.capture()["status"], "inactive")
            setattr(self, attribute, True)
        self.hooks["assistant_final"] = True
        self.assertEqual(self.capture()["status"], "inactive")
        self.hooks["assistant_final"] = False
        self.host._allows = lambda *args: False
        with self.assertRaises(CacheBlocked):
            self.capture()
        self.assertEqual(len(self.cloud.calls), calls)
        self.assertEqual(self.host.queue_status()["pending"], 1)

    def test_late_revocation_and_current_hook_gain_prevent_send_after_enqueue(self):
        enqueue = self.host._enqueue

        def revoked(request):
            row = enqueue(request)
            self.hooks["assistant_final"] = True
            return row

        self.host._enqueue = revoked
        self.assertEqual(self.capture()["local_queue"]["state"], "pending")
        self.assertEqual(self.cloud.calls, [])

    def test_native_scope_preserved_and_different_bindings_do_not_share_request_ids(
        self,
    ):
        self.context = {
            "origin": {
                "kind": "non-project",
                "cwd": "C:/synthetic/work",
                "project_root": None,
                "repository_root": None,
            },
            "session_id": "real-fixture-session",
        }
        self.capture()
        request = self.cloud.calls[-1]
        self.assertEqual(request["params"]["source_context"], self.context)
        self.host = RuleHost(self.root, self.cloud)
        self.host.queue_sha = "b" * 64
        self.core = self.restart()
        self.capture()
        self.assertNotEqual(self.cloud.calls[-1]["request_id"], request["request_id"])

    def test_recall_uses_approved_entry_and_hook_ownership_suppresses_manual_call(self):
        request = {
            "request_id": "synthetic_rule_recall",
            "method": "memory_query",
            "params": {
                "kind": "memory",
                "query": "Synthetic query",
                "since": None,
                "until": None,
                "limit": 5,
            },
        }
        calls = []

        def invoke(value):
            calls.append(value)
            return {"status": "synthetic-read"}

        self.assertEqual(
            self.core.recall(request, source_context=self.context, invoke=invoke),
            {"status": "synthetic-read"},
        )
        self.assertEqual(calls, [request])
        self.hooks["session_start"] = True
        self.assertEqual(
            self.core.recall(request, source_context=self.context, invoke=invoke)[
                "status"
            ],
            "inactive",
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.host.rows, {})
        with self.assertRaises(ValueError):
            self.core.recall(
                dict(request, method="memory_resume"),
                source_context=self.context,
                invoke=invoke,
            )

    def test_invalid_text_scope_and_source_gate_errors_fail_without_transfer(self):
        for context in (
            {},
            {"origin": None, "session_id": ""},
            {"origin": {"kind": "non-project"}, "session_id": None},
        ):
            with self.assertRaises(ValueError):
                self.core.capture(
                    "user_prompt",
                    "local-operation",
                    "Synthetic text",
                    representation="agent-summary",
                    source_context=context,
                )
        self.core.source_gate = lambda *args: 1
        self.assertEqual(self.capture()["status"], "inactive")

        def failed(*args):
            raise OSError("Synthetic source lookup failure")

        self.core.source_gate = failed
        self.assertEqual(self.capture()["status"], "inactive")
        self.assertEqual(self.host.rows, {})
        self.assertEqual(self.cloud.calls, [])


class DurableRuleOutbox(unittest.TestCase):
    """Exercise the real shared queue/storage on an isolated synthetic home."""

    def test_private_disk_queue_restart_and_generic_drain_never_send_rule_text(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir()
            cloud = Cloud()
            cloud.offline = True

            def restart():
                adapter = DiskRuleHost(home, root / "cache", cloud)
                core = RuleFallback(
                    adapter,
                    hooks=lambda: dict.fromkeys(CAPABILITIES, False),
                    enabled=lambda: True,
                    source_gate=lambda *args: True,
                )
                return adapter, core

            adapter, core = restart()

            def capture():
                return core.capture(
                    "user_prompt",
                    "persistent-operation",
                    "Synthetic observed user message",
                    representation="observed-user-message",
                    source_context={"origin": None, "session_id": None},
                )

            result = capture()
            self.assertEqual(result["local_queue"]["state"], "pending")
            path = next((root / "cache" / "outbox").glob("*.json"))
            original = path.read_bytes()
            request = json.loads(original)["request"]
            cache_mode(path, blocked=CacheBlocked)
            adapter, core = restart()
            cloud.offline = False
            with adapter._outbox_locked(adapter.cache / "content.lock"):
                adapter._drain()
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(cloud.puts, 0)
            result = capture()
            self.assertEqual(result["local_queue"]["state"], "confirmed")
            self.assertEqual(json.loads(path.read_bytes())["request"], request)
            self.assertEqual(cloud.calls, [request, request])
            self.assertEqual(cloud.puts, 1)

    def test_failed_durable_save_never_reaches_transport(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "home"
            home.mkdir()
            cloud = Cloud()
            adapter = DiskRuleHost(home, root / "cache", cloud)
            core = RuleFallback(
                adapter,
                hooks=lambda: dict.fromkeys(CAPABILITIES, False),
                enabled=lambda: True,
                source_gate=lambda *args: True,
            )
            with patch(
                "agent_brain_client.adapters.platforms.filesystem.os.fsync",
                side_effect=OSError("Synthetic durable save failure"),
            ):
                with self.assertRaises(OSError):
                    core.capture(
                        "assistant_final",
                        "persistent-operation",
                        "Synthetic summary",
                        representation="agent-summary",
                        source_context={"origin": None, "session_id": None},
                    )
            self.assertEqual(cloud.calls, [])
