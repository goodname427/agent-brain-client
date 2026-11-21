"""CodeBuddy stdout acknowledgements with synthetic recall and private homes."""

from contextlib import nullcontext
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_brain_client.adapters.harnesses.codebuddy import events
from agent_brain_client.adapters.harnesses.hook_output import hook_context
from agent_brain_client.adapters.platforms.filesystem import locked, private_directory
from agent_brain_client.adapters.storage import recall_receipts
from agent_brain_client.adapters.storage.collection import settings_path
from agent_brain_client.application.memory.lifecycle import handle
from agent_brain_client.application.memory.prompt_recall import recall_prompt
from agent_brain_client.interfaces.cli import codebuddy as cli


class ProcessInterrupted(BaseException):
    """A process exit bypasses ordinary hook error handling."""


class CodeBuddyOutputAck(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.value = {"device_home": str(self.home), "scope": {"synthetic": True}}
        self.payload = {"session_id": "synthetic-session", "turn_id": "synthetic-turn"}
        self.query = "Synthetic current question"
        self.retrieval = Mock(return_value=[{"content": "Synthetic useful memory", "layer": "L1"}])
        self.services = SimpleNamespace(
            settings_path=settings_path,
            locked=locked,
            cache_exists=lambda path: path.exists(),
            operation_stage=lambda _: nullcontext(),
            recall=self.retrieval,
            save_observation=recall_receipts.save_recall_observation,
        )
        private_directory(settings_path(self.home).parent)

    def native_hook(self, home, stream, *, core, observe, defer):
        self.assertEqual(home, self.home)
        items, state = handle(
            "UserPromptSubmit", prompt=self.query,
            stage=lambda: {"status": "staged"}, schedule=lambda: None,
            recall=lambda query: recall_prompt(
                self.value, {"cwd": str(self.home)}, self.payload, query,
                services=self.services, observe=observe,
            ),
        )
        return hook_context("UserPromptSubmit", items), state

    def run_cli(self, output, boundary=None):
        with (patch.object(sys, "argv", ["codebuddy", "hook", "--device-home", str(self.home)]),
              patch.object(cli, "verify_adapter"),
              patch.object(cli.host_binding, "load_core", return_value=SimpleNamespace()),
              patch.object(events, "hook", side_effect=self.native_hook),
              patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"{}"))),
              patch.object(sys, "stdout", output),
              patch.object(sys, "stderr", io.StringIO())):
            if boundary is None:
                return cli.main()
            with patch.object(output, boundary, side_effect=ProcessInterrupted):
                with self.assertRaises(ProcessInterrupted):
                    cli.main()

    def assert_retries_until_stdout_acknowledged(self, boundary):
        interrupted_output = io.StringIO()
        self.run_cli(interrupted_output, boundary)
        self.assertEqual(self.retrieval.call_count, 1)
        self.assertFalse(recall_receipts.recall_observation(self.value)["output_written"])
        self.assertFalse(list(settings_path(self.home).parent.glob("prompt-*.json")))
        output = io.StringIO()
        self.assertEqual(self.run_cli(output), 0)
        self.assertIn("additionalContext", output.getvalue())
        self.assertEqual(self.retrieval.call_count, 2)
        self.assertTrue(recall_receipts.recall_observation(self.value)["output_written"])
        self.assertEqual(len(list(settings_path(self.home).parent.glob("prompt-*.json"))), 1)
        duplicate = io.StringIO()
        self.assertEqual(self.run_cli(duplicate), 0)
        self.assertEqual(duplicate.getvalue().strip(), "{}")
        self.assertEqual(self.retrieval.call_count, 2)

    def test_write_interruption_does_not_suppress_same_turn_retry(self):
        self.assert_retries_until_stdout_acknowledged("write")

    def test_flush_interruption_does_not_suppress_same_turn_retry(self):
        self.assert_retries_until_stdout_acknowledged("flush")

    def test_failed_recall_never_acknowledges_output_or_suppresses_retry(self):
        self.retrieval.side_effect = ValueError("Synthetic recall failure")
        self.assertEqual(self.run_cli(io.StringIO()), 0)
        receipt = recall_receipts.recall_observation(self.value)
        self.assertEqual(receipt["status"], "failed")
        self.assertFalse(receipt["output_written"])
        self.assertFalse(list(settings_path(self.home).parent.glob("prompt-*.json")))
        self.retrieval.side_effect = None
        output = io.StringIO()
        self.assertEqual(self.run_cli(output), 0)
        self.assertIn("additionalContext", output.getvalue())
        self.assertTrue(recall_receipts.recall_observation(self.value)["output_written"])
        self.assertEqual(self.retrieval.call_count, 2)

    def test_failed_timing_is_opt_in_and_has_no_codebuddy_request_deadline(self):
        from agent_brain_client.application import control

        def recall(*args, **kwargs):
            with control.recall_response_headers(), control.recall_header_read(90):
                raise ValueError('Synthetic private failure detail')

        self.retrieval.side_effect = recall
        sample_file = settings_path(self.home).with_name('last-recall-timing.json')
        for enabled in ('0', '1'):
            with self.subTest(enabled=enabled), patch.dict(os.environ, {control.RECALL_TIMING_ENV: enabled}):
                output = io.StringIO()
                self.assertEqual(self.run_cli(output), 0)
                self.assertEqual(json.loads(output.getvalue()), {})
                receipt = recall_receipts.recall_observation(self.value)
                self.assertEqual(receipt['status'], 'failed')
                self.assertFalse(receipt['output_written'])
                self.assertEqual(sample_file.exists(), enabled == '1')
        sample_text = sample_file.read_text()
        sample = json.loads(sample_text)
        self.assertEqual(sample['first_read_budget_ms'], {'request_ms': None, 'socket_ms': 90000})
        self.assertNotIn('control_started', sample['phase_offsets_ms'])
        self.assertNotIn('Synthetic private failure detail', sample_text)

    def test_opt_in_success_timing_requires_flushed_stdout(self):
        from agent_brain_client.application import control

        sample_path = settings_path(self.home).with_name('last-recall-timing.json')
        with patch.dict(os.environ, {control.RECALL_TIMING_ENV: '1'}):
            self.run_cli(io.StringIO(), 'flush')
            self.assertFalse(sample_path.exists())
            self.assertEqual(self.run_cli(io.StringIO()), 0)
        sample = json.loads(sample_path.read_text())
        offsets = sample['phase_offsets_ms']
        self.assertLessEqual(offsets['output_prepared'], offsets['output_flushed'])
        self.assertLessEqual(offsets['output_flushed'], offsets['receipt_saved'])
        self.assertNotIn('failed', offsets)
        receipt = recall_receipts.recall_observation(self.value, sample['request_key'])
        self.assertTrue(receipt['output_written'])


if __name__ == "__main__":
    unittest.main()
