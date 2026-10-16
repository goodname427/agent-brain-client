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
        self.root = Path(self.tmp.name).resolve()
        self.old_queue_path = os.environ.get("AGENTBRAIN_NOTIFY_QUEUE_PATH")
        os.environ["AGENTBRAIN_NOTIFY_QUEUE_PATH"] = str(Path(self.tmp.name) / "queue.json")
        self.addCleanup(self._restore_queue_path)
        self.cwd = self.root / "synthetic-project"
        self.cwd.mkdir()
        self.codex_home = self.root / "synthetic-codex-home"
        (self.codex_home / "memories").mkdir(parents=True)
        (self.codex_home / ".agent-brain-client-synthetic-codex-home").write_text(
            "agent-brain-client synthetic Codex home v1\n", encoding="utf-8")
        self.old_codex_home = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.codex_home)
        self.addCleanup(self._restore_codex_home)
        self.event = json.dumps({
            "type": "agent-turn-complete",
            "thread-id": "synthetic-thread",
            "turn-id": "synthetic-turn",
            "cwd": str(self.cwd),
            "input-messages": ["Synthetic test data only."],
        }, separators=(",", ":"))
        self.old_callback = ["/mock/SkyComputerUseClient", "turn-ended"]
        self.agentbrain = ["/mock/python", "/mock/content_mcp.py", "--codex-notify"]
        self.argv = [
            "--callback-argv-json", json.dumps(self.old_callback),
            "--capture-argv-json", json.dumps(self.agentbrain),
            "--capture-cwd-allowlist-json", json.dumps([str(self.cwd)]),
            "--capture-codex-home", str(self.codex_home),
            self.event,
        ]

    def _restore_codex_home(self):
        if self.old_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self.old_codex_home

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

    def test_exact_allowed_cwd_enables_capture_and_preserves_callback_event(self):
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        self.assertEqual(dispatch_notify(self.argv, runner), 0)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args_list[0].args[0], self.old_callback + [self.event])

    def test_missing_or_mismatched_cwd_skips_capture_but_forwards_callback(self):
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        events = []
        event = json.loads(self.event)
        event.pop("cwd")
        events.append(json.dumps(event, separators=(",", ":")))
        event = json.loads(self.event)
        event["cwd"] = str(Path(self.tmp.name) / "other-project")
        events.append(json.dumps(event, separators=(",", ":")))
        for event_json in events:
            self.assertEqual(dispatch_notify(self.argv[:-1] + [event_json], runner), 0)
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args_list[0].args[0], self.old_callback + [events[0]])
        self.assertEqual(runner.call_args_list[1].args[0], self.old_callback + [events[1]])

    def test_non_synthetic_source_blocks_capture_but_preserves_callback(self):
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        (self.codex_home / ".agent-brain-client-synthetic-codex-home").unlink()
        self.assertEqual(dispatch_notify(self.argv, runner), 0)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.args[0], self.old_callback + [self.event])

    def test_nonempty_memories_or_codex_home_mismatch_blocks_capture(self):
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        source = self.codex_home / "memories" / "synthetic.md"
        source.write_text("synthetic", encoding="utf-8")
        self.assertEqual(dispatch_notify(self.argv, runner), 0)
        self.assertEqual(runner.call_count, 1)
        runner.reset_mock()
        source.unlink()
        os.environ["CODEX_HOME"] = str(Path(self.tmp.name) / "different-home")
        self.assertEqual(dispatch_notify(self.argv, runner), 0)
        self.assertEqual(runner.call_count, 1)

    def test_missing_capture_scope_options_fail_closed(self):
        runner = Mock(return_value=SimpleNamespace(returncode=0))
        argv = self.argv[:4] + [self.event]
        self.assertEqual(dispatch_notify(argv, runner), 0)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.args[0], self.old_callback + [self.event])
        runner.reset_mock()
        malformed_scope = self.argv[:-1]
        malformed_scope[5] = "not-json"
        self.assertEqual(dispatch_notify(malformed_scope + [self.event], runner), 0)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(runner.call_args.args[0], self.old_callback + [self.event])

    def test_failed_capture_is_not_replayed_against_a_later_turn(self):
        failed = Mock(side_effect=[SimpleNamespace(returncode=0),
                                   SimpleNamespace(returncode=2)])
        self.assertEqual(dispatch_notify(self.argv, failed), 1)
        second_event = json.dumps({"type": "agent-turn-complete",
                                   "thread-id": "synthetic-thread",
                                   "turn-id": "next-turn",
                                   "cwd": str(self.cwd),
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
