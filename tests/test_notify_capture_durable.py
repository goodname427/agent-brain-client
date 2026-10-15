"""Isolated durability tests for Codex notify capture jobs."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adapters/codex"))
from codex_notify_mux import NotifyCaptureQueue
from content_mcp import codex_notify_main


class NotifyCaptureDurableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "private" / "notify-jobs.json"

    @staticmethod
    def event(thread="thread-fixture", turn="turn-fixture", **extra):
        return {"type": "agent-turn-complete", "thread-id": thread,
                "turn-id": turn, "input-messages": ["PRIVATE RAW CONTENT"],
                "last-assistant-message": "PRIVATE RESPONSE", **extra}

    def test_enqueue_is_idempotent_and_never_persists_event_text(self):
        queue = NotifyCaptureQueue(self.ledger)
        event = self.event()
        first = queue.enqueue(event)
        second = queue.enqueue(dict(event))
        self.assertEqual(first, second)
        rows = queue.snapshot()
        self.assertEqual(len(rows), 1)
        serialized = self.ledger.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE RAW CONTENT", serialized)
        self.assertNotIn("PRIVATE RESPONSE", serialized)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["attempts"], 0)
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.ledger.parent.stat().st_mode & 0o777, 0o700)

    def test_new_process_can_claim_and_finish_durable_job(self):
        event = self.event()
        code = ("import json,sys; from codex_notify_mux import NotifyCaptureQueue; "
                "q=NotifyCaptureQueue(sys.argv[1]); print(q.enqueue(json.loads(sys.argv[2])))")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "adapters/codex")
        result = subprocess.run([sys.executable, "-c", code, str(self.ledger),
                                 json.dumps(event)], check=True, capture_output=True,
                                text=True, env=env)
        job_id = result.stdout.strip()
        fresh_process_queue = NotifyCaptureQueue(self.ledger)
        claimed = fresh_process_queue.claim_next()
        self.assertEqual(claimed["job_id"], job_id)
        self.assertEqual(claimed["attempts"], 1)
        self.assertTrue(fresh_process_queue.finish(job_id, success=True))
        self.assertEqual(fresh_process_queue.snapshot()[0]["status"], "done")

    def test_failure_and_unknown_result_survive_restart_and_are_retryable(self):
        now = [1000.0]
        queue = NotifyCaptureQueue(self.ledger, lease_seconds=30, clock=lambda: now[0])
        job_id = queue.enqueue(self.event())
        first_claim = queue.claim_next()
        self.assertTrue(queue.finish(job_id, success=False))
        self.assertEqual(queue.snapshot()[0]["status"], "pending")
        retry = NotifyCaptureQueue(self.ledger, lease_seconds=30, clock=lambda: now[0])
        second_claim = retry.claim_next()
        self.assertEqual(second_claim["attempts"], 2)
        # Simulate process death after dispatch: no finish/ack is written.
        now[0] += 31
        restarted = NotifyCaptureQueue(self.ledger, lease_seconds=30, clock=lambda: now[0])
        recovered = restarted.claim_next()
        self.assertEqual(recovered["job_id"], first_claim["job_id"])
        self.assertEqual(recovered["attempts"], 3)
        self.assertEqual(recovered["status"], "processing")

    def test_invalid_events_are_ignored_and_duplicate_identity_is_thread_scoped(self):
        queue = NotifyCaptureQueue(self.ledger)
        self.assertIsNone(queue.enqueue({"type": "approval-requested",
                                         "thread-id": "x", "turn-id": "y"}))
        internal = self.event(turn="internal")
        internal["input-messages"] = ["AGENTBRAIN_NOTIFY_CAPTURE:internal"]
        self.assertIsNone(queue.enqueue(internal))
        self.assertIsNone(queue.enqueue(self.event(thread="../private")))
        self.assertIsNone(queue.enqueue(self.event(turn="bad/id")))
        one = queue.enqueue(self.event(thread="thread-one", turn="same-turn"))
        two = queue.enqueue(self.event(thread="thread-two", turn="same-turn"))
        self.assertNotEqual(one, two)
        self.assertEqual(len(queue.snapshot()), 2)

    def test_bounded_ledger_prunes_old_done_but_never_drops_pending(self):
        queue = NotifyCaptureQueue(self.ledger, max_jobs=2)
        first = queue.enqueue(self.event(turn="one"))
        job = queue.claim_next()
        self.assertTrue(queue.finish(first, success=True))
        second = queue.enqueue(self.event(turn="two"))
        queue.enqueue(self.event(turn="three"))
        rows = queue.snapshot()
        self.assertEqual(len(rows), 2)
        self.assertNotIn(first, [row["job_id"] for row in rows])
        self.assertIn(second, [row["job_id"] for row in rows])
        with self.assertRaises(OSError):
            queue.enqueue(self.event(turn="four"))
        self.assertEqual(len(queue.snapshot()), 2)
        self.assertEqual(job["status"], "processing")

    def test_real_capture_callback_reports_missing_cli_as_failure(self):
        event = json.dumps(self.event())
        with patch.dict(os.environ, {"CODEX_CLI": "/missing/synthetic-codex"}):
            self.assertEqual(codex_notify_main([event], runner=Mock(side_effect=OSError())), 1)

    def test_real_capture_callback_reports_success_only_after_queue_accepts(self):
        event = json.dumps(self.event())
        with patch.dict(os.environ, {"CODEX_CLI": "/mock/synthetic-codex"}):
            runner = Mock(return_value=subprocess.CompletedProcess([], 0))
            self.assertEqual(codex_notify_main([event], runner=runner), 0)
            self.assertEqual(runner.call_count, 1)


if __name__ == "__main__":
    unittest.main()
