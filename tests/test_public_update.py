import agent_brain_client.interfaces.cli.install as interfaces_cli_install
import agent_brain_client.runtime.updates as runtime_updates
import agent_brain_client.adapters.storage.collection as source_policy
"""Public update actions with verified synthetic registration; no real device."""

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_client_uninstall as fixtures
import agent_brain_client.runtime.installation as wizard
import bootstrap.python_runtime as bootstrap
import agent_brain_client.runtime.collection as gm
import agent_brain_client.adapters.harnesses.codex.collection_installation as hooks


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
            def __init__(self, policy, digest, *, discovery_timeout=5):
                self.engine = SimpleNamespace(local=test.home / 'synthetic-packages')
                meta = json.loads(test.registration)
                test.assertEqual(
                    (policy, digest),
                    (meta["release_policy"], meta["release_policy_sha256"]),
                )

            def verified_active(self):
                active=copy.deepcopy(test.active)
                if active is not None:
                    # Reporting uses synthetic version/commit transitions, but
                    # offline tool discovery still inspects a real verified
                    # fixture package rather than an invented path or list.
                    active['package']=json.loads(test.registration)['package']
                return active

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
        # This fixture isolates channel reporting. Actual launcher transactions
        # have their own synthetic package/registration integration tests.
        import agent_brain_client.runtime.installation_completion as guide
        setup = patch.object(guide, 'source_setup', return_value={'collection_enabled':True})
        setup.start()
        self.addCleanup(setup.stop)
        handover = patch.object(runtime_updates, 'refresh_launcher', return_value=False)
        handover.start()
        self.addCleanup(handover.stop)

    def action(self, action):
        before_env = dict(os.environ)
        with (patch.object(
            self.f.f.backend, "load", side_effect=AssertionError("No credential access")
        ), patch.object(hooks, 'install', return_value={'status':'global-hook-installed','native_changed':False})):
            result = runtime_updates.manage_update(
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
        self.assertEqual(interfaces_cli_install.lifecycle_exit("check-update", result), 0)

    def test_explicit_maintenance_tolerates_a_six_second_fetch(self):
        class SlowChannel(self.channel):
            def __init__(self, policy, digest, *, discovery_timeout=5):
                super().__init__(policy, digest)
                self.timeout = discovery_timeout

            def discover(self, *args):
                if self.timeout < 6:
                    return {'status': 'release-unavailable', 'releases': []}
                return super().discover(*args)

            def update(self):
                if self.timeout < 6:
                    return {'status': 'offline'}
                return super().update()

        with (patch.object(runtime_updates, 'ClientReleases', SlowChannel),
              patch.object(self.f.f.backend, 'load', side_effect=AssertionError('No credential access')),
              patch.object(hooks, 'install', return_value={'status':'global-hook-installed','native_changed':False})):
            checked = runtime_updates.manage_update(self.home, 'check-update')
            self.assertEqual(checked['status'], 'client-update-available', checked)
            self.assertFalse(checked['active_package_changed'])
            updated = runtime_updates.manage_update(self.home, 'update')
            self.assertEqual(updated['status'], 'client-update-updated', updated)
        self.assertEqual(self.f.f.config.read_bytes(), self.native_before)
        self.assertEqual((self.f.state / 'codex-mcp.json').read_bytes(), self.registration)
        self.assertEqual(self.f.f.backend.record, self.identity_before)

    def test_update_and_repeat_use_existing_policy_and_require_observed_worker(self):
        result = self.action("update")
        self.assertEqual(result["status"], "client-update-updated", result)
        self.assertIsNone(result["reload_required"])
        self.assertEqual(result["reload_reason"], "running-worker-unobserved")
        self.assertIn("no MCP worker reload is needed", result["next_action"])
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
                self.assertEqual(interfaces_cli_install.lifecycle_exit(action, result), 2, result)
                self.assertEqual(result["active_package_version"], "0.2.10")
                self.assertIsNone(result["latest_channel_version"])
                self.assertFalse(result["channel_checked"])
                self.assertEqual(self.active["commit"], "a" * 40)

    def test_current_channel_still_refreshes_existing_hook_through_public_update(self):
        hooks.install(SimpleNamespace(device_home=str(self.home), profile_id=None, operator=None, operator_sha256=None))
        original = source_policy.load(self.home)
        self.latest = {'manifest': {'version': self.active['version']}, 'commit': self.active['commit']}
        self.discovery_status = 'current'
        self.update_status = 'current'
        with patch.object(self.f.f.backend, 'load', side_effect=AssertionError('No credential access')):
            checked = runtime_updates.manage_update(self.home, 'check-update', channel_factory=self.channel)
            self.assertTrue(checked['global_hook']['update_available'], checked)
            updated = runtime_updates.manage_update(self.home, 'update', channel_factory=self.channel)
        self.assertEqual(updated['status'], 'client-update-updated', updated)
        self.assertEqual(updated['global_hook']['status'], 'global-hook-updated')
        self.assertFalse(updated['active_package_changed'])
        self.assertTrue(updated['native_changed'])
        self.assertIsNone(updated['reload_required'])
        self.assertTrue(updated['reload_check']['hook_configuration_changed'])
        self.assertFalse(updated['reload_check']['mcp_registration_changed'])
        self.assertEqual(source_policy.load(self.home), original)

    def test_hook_failure_reports_partial_update_and_nonzero_exit(self):
        with patch.object(hooks, 'refresh', side_effect=ValueError('Synthetic Hook drift')):
            result = runtime_updates.manage_update(self.home, 'update', channel_factory=self.channel)
        self.assertEqual(result['reason'], 'global-hook-refresh-required', result)
        self.assertEqual(result['active_package_version'], '0.2.11')
        self.assertTrue(result['active_package_changed'])
        self.assertIsNone(result['native_changed'])
        self.assertEqual(interfaces_cli_install.lifecycle_exit('update', result), 2)

    def test_real_hook_conflict_reaches_public_receipt_with_exact_field(self):
        hooks.install(SimpleNamespace(device_home=str(self.home), profile_id=None, operator=None, operator_sha256=None))
        config = self.f.f.config
        config.write_bytes(config.read_bytes().replace(b'timeout = 120', b'timeout = 119'))
        changed = config.read_bytes()
        for action in ('check-update', 'update'):
            with self.subTest(action=action), patch.object(self.f.f.backend, 'load', side_effect=AssertionError('No credential access')):
                result = runtime_updates.manage_update(self.home, action, channel_factory=self.channel)
            self.assertEqual(result['reason'], 'managed-config-conflict', result)
            self.assertEqual(result['conflict_field'], 'config.hooks.SessionStart[0].hooks[0].timeout')
            self.assertIn(result['conflict_field'], result['next_action'])
            self.assertEqual(interfaces_cli_install.lifecycle_exit(action, result), 2)
            self.assertEqual(config.read_bytes(), changed)

    def test_mcp_conflict_receipt_names_table_without_echoing_values(self):
        config = self.f.f.config
        changed = config.read_bytes().replace(b'command = ', b'command = "synthetic-unapproved" # ')
        config.write_bytes(changed)
        result = runtime_updates.manage_update(self.home, 'check-update', channel_factory=self.channel)
        self.assertEqual(result['reason'], 'mcp-binding-changed', result)
        self.assertEqual(result['conflict_field'], 'mcp_servers.agent_brain_server')
        self.assertNotIn('synthetic-unapproved', json.dumps(result))
        self.assertEqual(config.read_bytes(), changed)

    def test_busy_editor_receipt_gives_retry_action_without_claiming_conflict(self):
        from agent_brain_client.adapters.storage.host_files import HostConfigChanged
        for action in ('check-update', 'update'):
            with self.subTest(action=action), patch.object(hooks, 'refresh', side_effect=HostConfigChanged('synthetic-private-detail')):
                result = runtime_updates.manage_update(self.home, action, channel_factory=self.channel)
            self.assertEqual(result['reason'], 'config-edit-in-progress', result)
            self.assertNotIn('conflict_field', result)
            self.assertIn('editor finish', result['next_action'])
            self.assertNotIn('synthetic-private-detail', json.dumps(result))

    def test_public_update_recovers_torn_hook_before_registration_inspection(self):
        from test_windows_install_closure import torn_hook
        hooks.install(SimpleNamespace(device_home=str(self.home), profile_id=None, operator=None, operator_sha256=None))
        torn_hook(self, self.home, self.f.native)
        torn = self.f.f.config.read_bytes()
        calls = []
        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 2)
        with patch.object(sys, 'executable', str(self.home / 'synthetic-other-python')):
            self.assertEqual(runtime_updates.reexec_update_runtime(self.home, 'update', runner=runner), 2)
        self.assertEqual(calls[0][0][0], self.f.f.materials['dependencies']['python'])
        self.assertEqual(calls[0][1]['env']['CODEX_HOME'], str(self.f.native))
        self.assertEqual(self.f.f.config.read_bytes(), torn)
        checked = runtime_updates.manage_update(self.home, 'check-update', channel_factory=self.channel)
        self.assertEqual(checked['reason'], 'global-hook-recovery-required', checked)
        output = io.StringIO()
        with (patch.object(Path, 'home', return_value=self.home),
              patch.object(sys, 'argv', ['agent_brain_client.runtime.installation.py', '--action', 'update']),
              patch.object(runtime_updates, 'ClientReleases', self.channel),
              patch.object(sys, 'stdout', output)):
            code = interfaces_cli_install.main()
        value = json.loads(output.getvalue())
        self.assertEqual(code, 0, value)
        self.assertEqual(value['status'], 'client-update-updated', value)
        self.assertTrue(value['reload_check']['hook_configuration_changed'])
        self.assertTrue(hooks.installation_status(source_policy.load(self.home)))

    def test_cli_does_not_resolve_connection_pair_or_install(self):
        output = io.StringIO()
        with (
            patch.object(Path, "home", return_value=self.home),
            patch.object(
                sys, "argv", ["agent_brain_client.runtime.installation.py", "--action", "check-update"]
            ),
            patch.object(sys, "stdout", output),
            patch.object(runtime_updates, "ClientReleases", self.channel),
            patch.object(
                wizard,
                "resolve_connection",
                side_effect=AssertionError("No connection resolution"),
            ),
        ):
            code = interfaces_cli_install.main()
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
                    "agent_brain_client.runtime.installation.py",
                    "--action",
                    "update",
                    "--connection",
                    "synthetic-untrusted.json",
                ],
            ),
            patch.object(sys, "stdout", output),
            patch.object(
                runtime_updates,
                "manage_update",
                side_effect=AssertionError("No replacement policy"),
            ),
        ):
            code = interfaces_cli_install.main()
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
            code = runtime_updates.reexec_update_runtime(self.home, "update", runner=runner)
        self.assertEqual(code, 2)
        argv, options = calls[0]
        self.assertEqual(argv[0], self.f.f.materials["dependencies"]["python"])
        self.assertEqual(argv[-2:], ["--action", "update"])
        self.assertEqual(options["env"]["CODEX_HOME"], str(self.f.native))
        self.assertEqual(self.f.f.config.read_bytes(), self.native_before)

    def test_runtime_failure_reports_update_review_without_install_fallback(self):
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["agent_brain_client.runtime.installation.py", "--action", "update"]),
            patch.object(sys, "stdout", output),
            patch.object(
                runtime_updates,
                "reexec_update_runtime",
                side_effect=ValueError("SYNTHETIC_PRIVATE_RUNTIME"),
            ),
            patch.object(
                wizard, "run", side_effect=AssertionError("No install fallback")
            ),
        ):
            code = interfaces_cli_install.main()
        result = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "client-update-review-required")
        self.assertFalse(result["channel_checked"])
        self.assertNotIn("SYNTHETIC_PRIVATE", output.getvalue())

    def test_unverified_post_update_state_does_not_report_previous_version_as_current(
        self,
    ):
        def failed_update(channel):
            self.active = None
            return {"status": "release-review-required"}

        with patch.object(self.channel, "update", failed_update):
            result = self.action("update")
        self.assertEqual(result["status"], "client-update-review-required")
        self.assertIsNone(result["active_package_version"])
        self.assertIsNone(result["latest_channel_version"])
        self.assertIn("No active package was verified", result["next_action"])

    def test_repeating_install_points_to_public_update_without_updater(self):
        with patch.object(
            runtime_updates.ClientReleases,
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
