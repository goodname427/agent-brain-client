"""Shared scheduling, policy persistence and finite operations in a fake home."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from agent_brain_client.adapters.storage import background as state
from agent_brain_client.interfaces.cli import install
from agent_brain_client.interfaces.mcp.launcher import BackgroundTasks
from agent_brain_client.runtime.background import software_update


class BackgroundMaintenance(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.clock = Mock(return_value=1000)
        self.release = Mock()
        self.release.update.return_value = {"status": "current"}
        self.current = Mock(return_value=True)

    def update(self):
        return software_update(self.home, self.release, "synthetic-policy",
                               still_current=self.current, clock=self.clock)["status"]

    def test_default_enable_disable_and_absolute_pause_survive_separate_reads(self):
        self.assertEqual(state.manage_policy(self.home, clock=self.clock)["policy"], "enabled")
        self.assertFalse((self.home / ".config").exists())
        state.manage_policy(self.home, "disable", clock=self.clock)
        state.manage_policy(self.home, "pause-day", clock=self.clock)
        self.clock.return_value += 86500
        self.assertEqual(self.update(), "disabled")
        self.release.update.assert_not_called()

        state.manage_policy(self.home, "enable", clock=self.clock)
        until = datetime.fromtimestamp(self.clock.return_value + 90, timezone.utc).isoformat()
        paused = state.manage_policy(self.home, "pause", until, clock=self.clock)
        self.assertEqual(paused["paused_until"], until)
        self.assertEqual(self.update(), "paused")
        self.clock.return_value += 90
        self.assertEqual(self.update(), "current")
        self.release.update.assert_called_once()
        self.assertEqual(state.manage_policy(self.home, clock=self.clock)["policy"], "enabled")

    def test_offline_attempt_is_coalesced_and_retries_next_round(self):
        self.release.update.side_effect = [{"status": "offline"}, {"status": "updated"}]
        self.assertEqual(self.update(), "offline")
        self.assertEqual(self.update(), "background-not-due")
        attempt = state.manage_policy(self.home, clock=self.clock)["last_check"]
        self.assertIsNone(attempt["succeeded_at"])
        self.clock.return_value += 3600
        self.assertEqual(self.update(), "updated")
        self.assertEqual(self.release.update.call_count, 2)
        self.assertEqual(state.manage_policy(self.home, clock=self.clock)["last_check"]["status"], "updated")

    def test_policy_edit_during_discovery_prevents_automatic_activation(self):
        def discovery(*, lock_timeout, can_activate):
            self.assertEqual(lock_timeout, 0)
            state.manage_policy(self.home, "disable", clock=self.clock)
            self.assertFalse(can_activate({}, "synthetic-package", {}))
            return {"status": "activation-deferred"}

        self.release.update.side_effect = discovery
        with patch("agent_brain_client.runtime.background.can_activate_at_startup", return_value=True):
            self.assertEqual(self.update(), "activation-deferred")
            self.assertEqual(self.update(), "disabled")
        self.release.update.assert_called_once()

    def test_device_update_attempt_has_one_owner_while_content_can_still_run(self):
        entered = threading.Event()
        resume = threading.Event()
        results = []

        def discovery(**_):
            entered.set()
            resume.wait(5)
            return {"status": "current"}

        self.release.update.side_effect = discovery
        worker = threading.Thread(target=lambda: results.append(self.update()))
        worker.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertEqual(self.update(), "background-busy")
            content = state.run_due(self.home / "cache/content.json", "synthetic-content", 60,
                                    lambda: "content-current", success=("content-current",))
            self.assertEqual(content["status"], "content-current")
        finally:
            resume.set()
            worker.join(5)
        self.assertEqual(results, ["current"])
        self.release.update.assert_called_once()

    def test_public_policy_entry_keeps_installed_and_running_versions_separate(self):
        output = io.StringIO()
        installation = {"registration_verified": True, "installed_version": "0.8.35"}
        with (
            patch("pathlib.Path.home", return_value=self.home),
            patch("agent_brain_client.adapters.installation.installation.inspect", return_value=installation),
            redirect_stdout(output),
        ):
            self.assertEqual(install.main(["--action", "auto-update", "--update-policy", "disable"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["policy"], "disabled")
        self.assertEqual(result["active_package_version"], "0.8.35")
        self.assertIsNone(result["running_worker_version"])
        self.assertEqual(self.update(), "disabled")

    def test_fixed_connection_tick_independently_dispatches_and_resyncs_on_release(self):
        started = []
        threads = {}

        class Pending:
            def __init__(self, *, target, args, **_):
                self.task = args[0]
                self.alive = True
                threads[self.task] = self

            def start(self):
                started.append(self.task)

            def is_alive(self):
                return self.alive

        tasks = BackgroundTasks(Mock(), clock=self.clock, thread=Pending)
        tasks.tick()
        self.assertEqual(started, ["background-content", "background-update"])
        threads["background-content"].alive = False
        self.clock.return_value += 30
        tasks.tick()
        self.assertEqual(len(started), 2)
        tasks.release_changed()
        tasks.tick()
        self.assertEqual(started[-1], "background-content")
        self.clock.return_value += 60
        tasks.tick()
        self.assertEqual(len(started), 3)
