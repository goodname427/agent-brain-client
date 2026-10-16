"""Unit tests for notify multiplexing; callback processes are always mocked."""

import json
import os
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adapters/codex"))
from codex_notify_mux import dispatch_notify


class CodexNotifyMuxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_queue_path = os.environ.get("AGENTBRAIN_NOTIFY_QUEUE_PATH")
        os.environ["AGENTBRAIN_NOTIFY_QUEUE_PATH"] = str(Path(self.tmp.name) / "queue.json")
        self.addCleanup(self._restore_queue_path)
        self.event = json.dumps({
            "type": "agent-turn-complete",
            "thread-id": "synthetic-thread",
            "turn-id": "synthetic-turn",
            "input-messages": ["Synthetic test data only."],
        }, separators=(",", ":"))
        self.old_callback = ["/mock/SkyComputerUseClient", "turn-ended"]
        self.agentbrain = ["/mock/python", "/mock/content_mcp.py", "--codex-notify"]
        self.argv = [
            "--callback-argv-json", json.dumps(self.old_callback),
            "--capture-argv-json", json.dumps(self.agentbrain),
            self.event,
        ]

    def _restore_queue_path(self):
        if self.old_queue_path is None:
            os.environ.pop("AGENTBRAIN_NOTIFY_QUEUE_PATH", None)
        else:
            os.environ["AGENTBRAIN_NOTIFY_QUEUE_PATH"] = self.old_queue_path

    def test_legacy_hook_preserves_callback_but_never_launches_deferred_capture(self):
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        self.assertEqual(dispatch_notify(self.argv, runner), 0)
        self.assertEqual(runner.call_count, 1)
        call = runner.call_args_list[0]
        self.assertEqual(call.args[0], self.old_callback + [self.event])
        self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(call.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(call.kwargs["timeout"], 8)
        self.assertFalse(call.kwargs["check"])
        self.assertFalse(Path(os.environ["AGENTBRAIN_NOTIFY_QUEUE_PATH"]).exists())

    def test_original_callback_failure_is_reported(self):
        runner = Mock(side_effect=OSError("synthetic launch failure"))
        self.assertEqual(dispatch_notify(self.argv, runner), 1)
        self.assertEqual(runner.call_count, 1)

    def test_nonzero_original_callback_is_reported_without_capture_call(self):
        runner = Mock(return_value=SimpleNamespace(returncode=3))
        self.assertEqual(dispatch_notify(self.argv, runner), 1)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args_list[0].args[0], self.old_callback + [self.event])

    def test_existing_legacy_hook_never_queues_capture_for_any_turn(self):
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        self.assertEqual(dispatch_notify(self.argv, runner), 0)
        second_event = json.dumps({"type": "agent-turn-complete",
                                   "thread-id": "synthetic-thread",
                                   "turn-id": "next-turn",
                                   "input-messages": ["another synthetic message"]})
        retry_argv = self.argv[:-1] + [second_event]
        self.assertEqual(dispatch_notify(retry_argv, runner), 0)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args_list[1].args[0], self.old_callback + [second_event])
        self.assertFalse(Path(os.environ["AGENTBRAIN_NOTIFY_QUEUE_PATH"]).exists())

    def test_previously_documented_scope_options_preserve_original_callback(self):
        scoped_argv = [
            "--callback-argv-json", json.dumps(self.old_callback),
            "--capture-argv-json", json.dumps(self.agentbrain),
            "--capture-cwd-allowlist-json", json.dumps(["/synthetic/project"]),
            "--capture-codex-home", "/synthetic/codex-home",
            self.event,
        ]
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        self.assertEqual(dispatch_notify(scoped_argv, runner), 0)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args_list[0].args[0], self.old_callback + [self.event])
        self.assertFalse(Path(os.environ["AGENTBRAIN_NOTIFY_QUEUE_PATH"]).exists())

    def test_invalid_event_or_command_configuration_runs_neither_callback(self):
        runner = Mock()
        self.assertEqual(dispatch_notify(self.argv[:-1] + ["not-json"], runner), 0)
        self.assertEqual(dispatch_notify(self.argv[:-3] + ["--callback-argv-json", "[]",
                                                            "--capture-argv-json", "[]",
                                                            self.event], runner), 0)
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
