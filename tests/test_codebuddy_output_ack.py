"""CodeBuddy stdout acknowledgements with synthetic recall and private homes."""

from contextlib import nullcontext
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_brain_client.adapters.harnesses.codebuddy import events
from agent_brain_client.adapters.harnesses import hook_output
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

    def native_hook(self, home, stream, *, core, observe, defer, verified_release=False):
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

    def test_failed_timing_is_opt_in_and_uses_remaining_codebuddy_request_budget(self):
        from agent_brain_client.application import control

        def recall(*args, **kwargs):
            with control.recall_response_headers(), control.recall_header_read(control.request_timeout(90)):
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
        budget = sample['first_read_budget_ms']
        self.assertGreater(budget['request_ms'], 0)
        self.assertLessEqual(budget['request_ms'], 29400)
        self.assertAlmostEqual(budget['request_ms'], budget['socket_ms'], delta=100)
        self.assertIn('control_started', sample['phase_offsets_ms'])
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



_PIPE_FILL = """
def fill_unread_pipe(descriptor):
    chunk = b"x"
    progress = threading.Condition()
    completed_writes = 0
    fill_limit = 1048576

    def fill():
        nonlocal completed_writes
        for _ in range(fill_limit):
            assert os.write(descriptor, chunk) == len(chunk)
            with progress:
                completed_writes += 1
                progress.notify()

    # Fill to the last byte before arming the watchdog. A blocked large
    # write can still leave room for a short timing record.
    feeder = threading.Thread(target=fill, daemon=True)
    feeder.start()
    with progress:
        assert progress.wait_for(lambda: completed_writes > 0, timeout=3)
        stop = time.monotonic() + 2
        while completed_writes < fill_limit and time.monotonic() < stop:
            previous = completed_writes
            if not progress.wait_for(lambda: completed_writes != previous, timeout=.15):
                break
        else:
            raise AssertionError("unread pipe did not fill")
        assert feeder.is_alive(), "pipe filler returned instead of blocking"
"""


class NativeDeadline(unittest.TestCase):
    def run_undrained(self, program, *args, env=None):
        src = Path(__file__).resolve().parents[1] / "src"
        with subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", program, str(src), *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        ) as process:
            try:
                status = process.wait(timeout=8)
            finally:
                if process.poll() is None:
                    process.kill()
                stdout, stderr = process.communicate()
        self.assertEqual(status, 0, stderr[-2048:])
        return stdout, stderr

    def test_unread_stdout_or_stderr_cannot_block_exit(self):
        child = """
import os
import sys
import threading
import time

sys.path.insert(0, sys.argv[1])
from agent_brain_client.adapters.harnesses.hook_output import emit, native_output_deadline
""" + _PIPE_FILL + """
pipe = sys.argv[2]
fill_unread_pipe(sys.stdout.fileno() if pipe == "stdout" else sys.stderr.fileno())
with native_output_deadline(.25):
    if pipe == "stdout":
        emit({"text": "x" * 16384})
        raise AssertionError("write returned while stdout was undrained")
    time.sleep(15)
"""
        for pipe in ("stdout", "stderr"):
            with self.subTest(pipe=pipe):
                stdout, stderr = self.run_undrained(child, pipe)
                saturated_stream = stdout if pipe == "stdout" else stderr
                self.assertEqual(saturated_stream[:512], b"x" * 512)
                if pipe == "stderr":
                    self.assertEqual(stdout.strip(), b"{}")

    def test_launcher_timing_exits_with_unread_stderr(self):
        launcher = """
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
from agent_brain_client.adapters.installation import launcher_update
from agent_brain_client.adapters.storage import registration
""" + _PIPE_FILL + """
home, saturation = sys.argv[2:]
active = {"package": home}
saved = {"release_policy": "synthetic", "release_policy_sha256": "b" * 64}
channel = SimpleNamespace(
    policy={"runtime": {"device_home": home}},
    verified_active=lambda *, serving: active,
)
if saturation == "before-child":
    fill_unread_pipe(sys.stderr.fileno())
with (patch.object(registration, "registration", return_value=(saved, {})),
      patch.object(launcher_update, "ClientReleases", return_value=channel)):
    sys.exit(launcher_update.native_hook_startup(home, home, "codebuddy", [], saved))
"""
        child = """
import os
import sys
import threading
import time
""" + _PIPE_FILL + """
def run_native_hook(home, harness):
    if os.environ["SYNTHETIC_PIPE_SATURATION"] == "after-child":
        fill_unread_pipe(sys.stderr.fileno())
    print("{}", flush=True)
    os._exit(0)
"""
        with tempfile.TemporaryDirectory(prefix="synthetic-hook-timing-") as tmp:
            home = Path(tmp).resolve()
            entry = home / "src/agent_brain_client/runtime/host_binding.py"
            entry.parent.mkdir(parents=True)
            entry.write_text(child, encoding="utf-8")
            for saturation in ("before-child", "after-child"):
                with self.subTest(saturation=saturation):
                    environment = dict(os.environ, AGENT_BRAIN_RECALL_TIMING="1",
                                       SYNTHETIC_PIPE_SATURATION=saturation)
                    stdout, stderr = self.run_undrained(launcher, str(home), saturation, env=environment)
                    self.assertEqual(stdout.strip(), b"{}")
                    self.assertIn(b"x" * 512, stderr)

    def test_flush_after_deadline_never_acknowledges_recall(self):
        clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 0.01, 1.01]))
        output = io.StringIO()
        with (patch.object(hook_output, "time", clock), patch.object(sys, "stdout", output),
              hook_output.native_output_deadline(1)):
            self.assertFalse(hook_output.emit({"synthetic": True}))
        self.assertEqual(json.loads(output.getvalue()), {"synthetic": True})


if __name__ == "__main__":
    unittest.main()
