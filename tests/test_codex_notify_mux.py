"""Unit tests for notify multiplexing; callback processes are always mocked."""

import json
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

    def test_both_callbacks_get_the_exact_original_event_after_their_fixed_args(self):
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        self.assertEqual(dispatch_notify(self.argv, runner), 0)
        self.assertEqual(runner.call_count, 2)
        first = runner.call_args_list[0]
        second = runner.call_args_list[1]
        self.assertEqual(first.args[0], self.old_callback + [self.event])
        self.assertEqual(second.args[0], self.agentbrain + [self.event])
        for call in (first, second):
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(call.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(call.kwargs["timeout"], 8)
            self.assertFalse(call.kwargs["check"])

    def test_original_callback_failure_does_not_suppress_agentbrain(self):
        runner = Mock(side_effect=[OSError("synthetic launch failure"),
                                  SimpleNamespace(returncode=0)])
        self.assertEqual(dispatch_notify(self.argv, runner), 1)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args_list[1].args[0], self.agentbrain + [self.event])

    def test_agentbrain_failure_does_not_suppress_original_callback(self):
        runner = Mock(side_effect=[SimpleNamespace(returncode=0),
                                  subprocess.TimeoutExpired("mock", 8)])
        self.assertEqual(dispatch_notify(self.argv, runner), 1)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args_list[0].args[0], self.old_callback + [self.event])

    def test_nonzero_from_either_is_reported_after_both_callbacks_run(self):
        runner = Mock(side_effect=[SimpleNamespace(returncode=3),
                                  SimpleNamespace(returncode=0)])
        self.assertEqual(dispatch_notify(self.argv, runner), 1)
        self.assertEqual(runner.call_count, 2)

    def test_invalid_event_or_command_configuration_runs_neither_callback(self):
        runner = Mock()
        self.assertEqual(dispatch_notify(self.argv[:-1] + ["not-json"], runner), 0)
        self.assertEqual(dispatch_notify(self.argv[:-3] + ["--callback-argv-json", "[]",
                                                            "--capture-argv-json", "[]",
                                                            self.event], runner), 0)
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
