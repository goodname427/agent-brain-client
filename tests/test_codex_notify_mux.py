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
from codex_notify_mux import NotifyCaptureQueue, dispatch_notify


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

    def test_both_callbacks_get_the_exact_original_event_after_their_fixed_args(self):
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        self.assertEqual(dispatch_notify(self.argv, runner), 0)
        self.assertEqual(runner.call_count, 2)
        first = runner.call_args_list[0]
        second = runner.call_args_list[1]
        self.assertEqual(first.args[0], self.old_callback + [self.event])
        self.assertEqual(second.args[0][:-1], self.agentbrain)
        capture_event = json.loads(second.args[0][-1])
        self.assertEqual(capture_event["thread-id"], "synthetic-thread")
        self.assertEqual(capture_event["turn-id"], "synthetic-turn")
        self.assertEqual(capture_event["input-messages"], ["validated notify input"])
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
        self.assertEqual(runner.call_args_list[1].args[0][:-1], self.agentbrain)

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

    def test_failed_capture_is_not_replayed_against_a_later_turn(self):
        failed = Mock(side_effect=[SimpleNamespace(returncode=0),
                                   SimpleNamespace(returncode=2)])
        self.assertEqual(dispatch_notify(self.argv, failed), 1)
        second_event = json.dumps({"type": "agent-turn-complete",
                                   "thread-id": "synthetic-thread",
                                   "turn-id": "next-turn",
                                   "input-messages": ["another synthetic message"]})
        retry_argv = self.argv[:-1] + [second_event]
        succeeded = Mock(return_value=SimpleNamespace(returncode=0))
        self.assertEqual(dispatch_notify(retry_argv, succeeded), 0)
        retried_capture = json.loads(succeeded.call_args_list[1].args[0][-1])
        self.assertEqual(retried_capture["turn-id"], "next-turn")
        jobs = NotifyCaptureQueue(os.environ["AGENTBRAIN_NOTIFY_QUEUE_PATH"]).snapshot()
        self.assertEqual([job["status"] for job in jobs], ["failed", "done"])
        self.assertEqual(jobs[0]["attempts"], 1)

    def test_invalid_event_or_command_configuration_runs_neither_callback(self):
        runner = Mock()
        self.assertEqual(dispatch_notify(self.argv[:-1] + ["not-json"], runner), 0)
        self.assertEqual(dispatch_notify(self.argv[:-3] + ["--callback-argv-json", "[]",
                                                            "--capture-argv-json", "[]",
                                                            self.event], runner), 0)
        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
