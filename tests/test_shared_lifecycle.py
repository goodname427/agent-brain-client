"""Shared behavior with synthetic durable queues and no native or server access."""

import unittest
from unittest.mock import Mock
from agent_brain_client.application.memory.lifecycle import handle
from agent_brain_client.application.memory.observations import stage_messages
import test_content_outbox as fixture


class Lifecycle(unittest.TestCase):
    def test_same_order_for_both_harnesses(self):
        for host in ("codex", "codebuddy"):
            with self.subTest(host=host):
                calls = []
                items, state = handle("UserPromptSubmit", prompt="synthetic question",
                    stage=lambda: calls.append("stage") or {"status": "staged"},
                    schedule=lambda: calls.append("schedule"),
                    recall=lambda query: calls.append("recall") or [{"content": "synthetic"}])
                self.assertEqual(calls, ["stage", "schedule", "recall"])
                self.assertEqual(items, [{"content": "synthetic"}])
                self.assertEqual(set(state["timing_ms"]), {"stage_ms", "schedule_ms", "recall_ms", "total_ms"})
                self.assertNotIn("question", str(state))

    def test_start_only_schedules_and_stop_never_recalls(self):
        stage, schedule, recall = Mock(return_value={"status": "staged"}), Mock(), Mock()
        self.assertEqual(handle("SessionStart", stage=stage, schedule=schedule, recall=recall)[0], [])
        stage.assert_not_called()
        recall.assert_not_called()
        handle("Stop", stage=stage, schedule=schedule, recall=recall)
        stage.assert_called_once()
        recall.assert_not_called()

    def test_capture_or_scheduler_failure_does_not_remove_recall(self):
        result, status = handle("UserPromptSubmit", prompt="synthetic question",
            stage=Mock(side_effect=OSError()), schedule=Mock(side_effect=RuntimeError()),
            recall=lambda q: [{"content": "synthetic"}])
        self.assertTrue(result)
        self.assertEqual(status["status"], "capture-unconfirmed")
        self.assertEqual(status["delivery_schedule"], "pending")


class Observations(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.Outbox()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.queue = self.fixture.adapter

    def stage(self, phase="prompt", operation="turn-1", text="synthetic observation"):
        return stage_messages(self.queue, project_id="synthetic-project", session_id="synthetic-session",
            operation_id=operation, phase=phase,
            messages=[{"role": "user" if phase == "prompt" else "assistant", "content": text}])

    def test_prompt_and_final_separate_durable_no_network(self):
        first = self.stage()
        second = self.stage("assistant")
        self.assertEqual(first["status"], "staged")
        self.assertEqual(second["local_queue"], {"saved": True, "state": "pending"})
        self.assertFalse(self.fixture.cloud.calls)
        self.assertEqual(len(self.queue._queued()), 2)
        self.queue = self.fixture.restart()
        self.assertEqual(self.stage()["request_ids"], first["request_ids"])
        self.assertEqual(len(self.queue._queued()), 2)

    def test_same_text_new_turn_is_not_collapsed_and_changed_operation_rejected(self):
        self.stage()
        self.stage(operation="turn-2")
        with self.assertRaises(ValueError):
            self.stage(text="changed observation")
        self.assertEqual(len(self.queue._queued()), 2)

    def test_long_unicode_split_before_background_delivery(self):
        text = "測試😀" * 7000
        result = self.stage(text=text)
        rows = {r["request"]["request_id"]: r for r in self.queue._queued()}
        messages = [m["content"] for key in result["request_ids"] for m in rows[key]["request"]["params"]["event"]["messages"]]
        self.assertEqual("".join(messages), text)
        self.assertTrue(all(len(m.encode("utf-16-le")) // 2 <= 8192 for m in messages))
        self.assertFalse(self.fixture.cloud.calls)

    def test_changed_tail_or_removed_batch_cannot_reuse_operation(self):
        self.stage(text="synthetic " * 20000)
        with self.assertRaises(ValueError):
            self.stage(text="synthetic " * 10000)
        self.assertFalse(self.fixture.cloud.calls)


class PromptRecall(unittest.TestCase):
    def test_locks_are_scoped_to_request_not_all_sessions(self):
        from contextlib import nullcontext
        from pathlib import Path
        from types import SimpleNamespace
        from agent_brain_client.application.memory.prompt_recall import recall_prompt
        locks = []
        services = SimpleNamespace(settings_path=lambda home: Path('/synthetic/settings.json'),
            locked=lambda path: locks.append(path) or nullcontext(), cache_exists=lambda path: False,
            operation_stage=lambda stage: nullcontext(), recall=lambda *args, **kwargs: [],
            write=Mock(), save_observation=Mock())
        value = {'device_home': '/synthetic', 'scope': {'synthetic': True}}
        for session in ('session-a', 'session-b'):
            recall_prompt(value, {'cwd': '/synthetic/project'}, {'session_id': session, 'turn_id': 'turn'},
                          'synthetic question', services=services)
        self.assertEqual(len(set(locks)), 2)
        self.assertTrue(all(path.name.startswith('prompt-recall-') for path in locks))
        self.assertEqual(services.save_observation.call_count, 2)
