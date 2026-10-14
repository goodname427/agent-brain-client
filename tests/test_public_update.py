"""Public update actions with verified synthetic registration; no real device."""

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_client_uninstall as fixtures
import mac_first_install as wizard
import prepare_install_runtime as bootstrap


class PublicUpdate(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ClientUninstall(
            "test_cancel_is_read_only_and_status_never_reads_credentials"
        )
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.home = self.f.home
        self.active = {"version": "0.2.10", "commit": "a" * 40}
        self.latest = {"manifest": {"version": "0.2.11"}, "commit": "b" * 40}
        self.calls = []
        self.discovery_status = "compatible-update"
        self.update_status = "updated"
        self.native_before = self.f.f.config.read_bytes()
        self.identity_before = copy.deepcopy(self.f.identity)
        self.registration = (self.f.state / "codex-mcp.json").read_bytes()
        test = self

        class Channel:
            def __init__(self, policy, digest):
                meta = json.loads(test.registration)
                test.assertEqual(
                    (policy, digest),
                    (meta["release_policy"], meta["release_policy_sha256"]),
                )

            def verified_active(self):
                return copy.deepcopy(test.active)

            def discover(self, adapter, platform, installed):
                test.calls.append(("discover", adapter, platform, installed))
                return {
                    "status": test.discovery_status,
                    "releases": [test.latest]
                    if test.discovery_status in ("compatible-update", "current")
                    else [],
                }

            def update(self):
                test.calls.append(("update",))
                if test.update_status == "updated":
                    test.active = {
                        "version": test.latest["manifest"]["version"],
                        "commit": test.latest["commit"],
                    }
                return {"status": test.update_status}

        self.channel = Channel

    def action(self, action):
        before_env = dict(os.environ)
        with patch.object(
            self.f.f.backend, "load", side_effect=AssertionError("No credential access")
        ):
            result = wizard.manage_update(
                self.home, action, channel_factory=self.channel
            )
        self.assertEqual(dict(os.environ), before_env)
        self.assertEqual(self.f.f.config.read_bytes(), self.native_before)
        self.assertEqual(
            (self.f.state / "codex-mcp.json").read_bytes(), self.registration
        )
        self.assertEqual(self.f.f.backend.record, self.identity_before)
        return result

    def test_check_discovers_pinned_channel_without_activating_or_claiming_worker(self):
        before = copy.deepcopy(self.active)
        result = self.action("check-update")
        self.assertEqual(result["status"], "client-update-available", result)
        self.assertEqual(self.active, before)
        self.assertEqual(self.calls, [("discover", "codex", "darwin-arm64", "0.2.10")])
        self.assertEqual(result["latest_channel_version"], "0.2.11")
        self.assertEqual(result["active_package_version"], "0.2.10")
        self.assertIsNone(result["running_worker_version"])
        self.assertFalse(result["active_package_changed"])
        self.assertEqual(wizard.lifecycle_exit("check-update", result), 0)

    def test_update_and_repeat_use_existing_policy_and_require_observed_worker(self):
        result = self.action("update")
        self.assertEqual(result["status"], "client-update-updated", result)
        self.assertTrue(result["reload_required"])
        self.assertEqual(result["active_package_version"], "0.2.11")
        self.assertIsNone(result["running_worker_version"])
        self.update_status = "current"
        repeated = self.action("update")
        self.assertEqual(repeated["status"], "client-update-current", repeated)
        self.assertIsNone(repeated["reload_required"])
        self.assertEqual(self.calls, [("update",), ("update",)])

    def test_channel_failure_or_health_rejection_does_not_claim_latest_or_success(self):
        for action, failure in [
            ("check-update", "release-unavailable"),
            ("update", "offline"),
            ("update", "rolled-back"),
            ("update", "release-review-required"),
        ]:
            with self.subTest(action=action, failure=failure):
                self.discovery_status = failure
                self.update_status = failure
                result = self.action(action)
                self.assertEqual(wizard.lifecycle_exit(action, result), 2, result)
                self.assertEqual(result["active_package_version"], "0.2.10")
                self.assertIsNone(result["latest_channel_version"])
                self.assertFalse(result["channel_checked"])
                self.assertEqual(self.active["commit"], "a" * 40)

    def test_cli_does_not_resolve_connection_pair_or_install(self):
        output = io.StringIO()
        with (
            patch.object(Path, "home", return_value=self.home),
            patch.object(
                sys, "argv", ["mac_first_install.py", "--action", "check-update"]
            ),
            patch.object(sys, "stdout", output),
            patch.object(wizard, "ClientReleases", self.channel),
            patch.object(
                wizard,
                "resolve_connection",
                side_effect=AssertionError("No connection resolution"),
            ),
        ):
            code = wizard.main()
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(
            json.loads(output.getvalue())["status"], "client-update-available"
        )

    def test_cli_rejects_replacement_connection_before_any_update(self):
        output = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "mac_first_install.py",
                    "--action",
                    "update",
                    "--connection",
                    "synthetic-untrusted.json",
                ],
            ),
            patch.object(sys, "stdout", output),
            patch.object(
                wizard,
                "manage_update",
                side_effect=AssertionError("No replacement policy"),
            ),
        ):
            code = wizard.main()
        self.assertEqual(code, 2)
        self.assertEqual(
            json.loads(output.getvalue())["reason"], "existing-policy-only"
        )

    def test_operator_runtime_reexecutes_original_pinned_python_without_rebinding(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 2)

        with patch.object(sys, "executable", str(self.home / "synthetic-other-python")):
            code = wizard.reexec_update_runtime(self.home, "update", runner=runner)
        self.assertEqual(code, 2)
        argv, options = calls[0]
        self.assertEqual(argv[0], self.f.f.materials["dependencies"]["python"])
        self.assertEqual(argv[-2:], ["--action", "update"])
        self.assertEqual(options["env"]["CODEX_HOME"], str(self.f.native))
        self.assertEqual(self.f.f.config.read_bytes(), self.native_before)

    def test_repeating_install_points_to_public_update_without_updater(self):
        with patch.object(
            wizard.ClientReleases,
            "update",
            side_effect=AssertionError("Install is not update"),
        ):
            result = self.f.f.run_wizard()
        self.assertTrue(result["installed"], result)
        self.assertFalse(result["update_check_performed"])
        self.assertIn("--action check-update", result["next_action"])
        self.assertIn("--action update", result["next_action"])

    def test_maintenance_does_not_prepare_missing_dependencies(self):
        empty = self.f.f.base / "empty-home"
        empty.mkdir()
        with (
            patch.object(sys, "version_info", (3, 9)),
            patch.object(
                bootstrap, "download", side_effect=AssertionError("No preparation")
            ),
        ):
            with self.assertRaises(ValueError):
                bootstrap.runtime(empty, prepare=False)
        self.assertEqual(list(empty.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
