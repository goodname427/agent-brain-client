"""Synthetic install/use/remove/reinstall; no real Keychain, server or Codex."""
import copy
import json
import unittest
from unittest.mock import patch

import test_mac_first_install as fixture
from test_content_outbox import Cloud, MEMORY, RULE, SKILL
from content_cache import ContentAdapter, BEGIN
from https_content import HTTPSContentClient
from brain_lock import locked, PlatformUnsupported
import content_install as install
import client_lifecycle as lifecycle


class ClientUninstall(unittest.TestCase):
    def setUp(self):
        self.f = fixture.FirstInstall('test_first_install_prepared_environment_and_exact_native_transaction')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.fixture()
        self.assertTrue(self.f.run_wizard()['installed'])
        self.home, self.native = self.f.home, self.f.native
        self.state = self.home / '.config/agent-brain-client'
        self.preparation = self.state / 'first-install'
        self.active = patch('client_release.ClientReleases.verified_active', return_value={'version': '0.2.5'})
        self.active.start()
        self.addCleanup(self.active.stop)
        self.cloud = Cloud()
        self.cloud.grant = copy.deepcopy(self.f.fixture_grant)
        policy = install.private_json(self.preparation / 'release-policy.json')
        runtime = policy['runtime']
        client = HTTPSContentClient(runtime['https_profile'], runtime['https_profile_sha256'], store=self.f.store)
        self.cloud.queue_identity = client.queue_identity
        self.adapter = ContentAdapter(self.home, runtime['content_cache'], install.private_json(self.preparation / 'content-policy.json'), self.cloud, codex_home=self.native)
        (self.native / 'AGENTS.md').write_text('User instruction preserved.\n')
        (self.native / 'AGENTS.md').chmod(0o600)
        self.cloud.records = {('memory', 'synthetic-memory'): copy.deepcopy(MEMORY), ('rule', 'synthetic-rule'): dict(RULE, enabled=True), ('skill', 'synthetic-skill'): dict(SKILL, enabled=True)}
        self.assertEqual(self.adapter.refresh()['status'], 'content-refreshed')
        self.identity = copy.deepcopy(self.f.backend.record)

    def remove(self, **kwargs):
        return lifecycle.uninstall(self.home, kwargs.get('confirm', lambda text: 'Continue'))

    def request(self, rid='synthetic_lifecycle_write_0001'):
        return {'request_id': rid, 'method': 'content_put', 'params': {'kind': 'memory', 'id': 'synthetic-memory', 'record': dict(MEMORY, body='Synthetic updated body'), 'expected_revision': install.contract.envelope('memory', 'synthetic-memory', MEMORY)['revision']}}

    def test_read_write_remove_reinstall_retains_identity_and_confirmed_outbox(self):
        read = self.adapter.invoke({'request_id': 'synthetic_lifecycle_read_0001', 'method': 'content_read', 'params': {'kind': 'memory', 'id': 'synthetic-memory'}})
        self.assertEqual(read['result']['record']['body'], MEMORY['body'])
        self.assertTrue(self.adapter.invoke(self.request())['persistence']['durable'])
        queue = self.adapter.cache / 'outbox/synthetic_lifecycle_write_0001.json'
        queued_bytes = queue.read_bytes()
        self.f.config.write_text(self.f.config.read_text() + '\nexternal_setting="keep"\n')
        # Appending a setting to an MCP table is scope drift; put the setting at root.
        self.f.config.write_text('external_setting="keep"\n' + self.f.config.read_text().replace('\nexternal_setting="keep"\n', '\n'))
        result = self.remove()
        self.assertEqual(result['status'], 'client-uninstalled', result)
        self.assertNotIn(install.NAME, install.tomllib.loads(self.f.config.read_text()).get('mcp_servers', {}))
        self.assertEqual(install.tomllib.loads(self.f.config.read_text())['external_setting'], 'keep')
        self.assertFalse((self.state / 'codex-mcp.json').exists())
        self.assertNotIn(BEGIN, (self.native / 'AGENTS.md').read_text())
        self.assertIn('User instruction preserved.', (self.native / 'AGENTS.md').read_text())
        self.assertFalse((self.home / '.agents/skills/synthetic-skill').exists())
        self.assertFalse((self.home / '.config/agent-brain-client/content/memory/synthetic-memory.md').exists())
        self.assertEqual(queue.read_bytes(), queued_bytes)
        self.assertEqual(self.f.backend.record, self.identity)
        self.assertEqual(lifecycle.status(self.home)['status'], 'client-uninstalled')
        self.assertFalse(self.remove()['native_changed'])
        result = self.f.run_wizard(updater=lambda: {'status': 'current', 'active': {'version': '0.2.5'}})
        self.assertTrue(result['installed'], result)
        self.assertTrue(install.inspect(self.home)['installed'])
        self.assertEqual(self.f.prompts, 1)
        self.assertEqual(self.f.backend.record, self.identity)
        self.assertEqual(queue.read_bytes(), queued_bytes)
        self.assertEqual(self.adapter.refresh()['status'], 'content-refreshed')
        self.assertTrue((self.home / '.agents/skills/synthetic-skill/SKILL.md').exists())
        self.assertEqual(self.remove()['status'], 'client-uninstalled')
        self.assertTrue(self.f.run_wizard()['installed'])
        self.assertEqual(self.f.prompts, 1)
        receipt = lifecycle.read_removal(self.home)
        self.assertNotIn(self.identity['credential'], json.dumps(receipt))

    def test_cancel_is_read_only_and_status_never_reads_credentials(self):
        before = self.f.config.read_bytes()
        with patch.object(self.f.backend, 'load', side_effect=AssertionError('Credential access forbidden')):
            self.assertEqual(lifecycle.status(self.home)['queue']['pending'], 0)
            self.assertEqual(self.remove(confirm=lambda text: 'Cancel')['status'], 'uninstall-cancelled')
        self.assertEqual(self.f.config.read_bytes(), before)
        self.assertFalse(lifecycle.journal_path(self.home).exists())

    def test_pending_blocks_then_original_id_recovery_allows_removal(self):
        self.cloud.offline = True
        response = self.adapter.invoke(self.request())
        self.assertFalse(response['persistence']['durable'])
        before = self.f.config.read_bytes()
        result = self.remove()
        self.assertEqual(result['status'], 'uninstall-unconfirmed-writes')
        self.assertEqual(result['queue']['pending'], 1)
        self.assertEqual(self.f.config.read_bytes(), before)
        self.cloud.offline = False
        self.adapter.refresh()
        self.assertEqual(self.adapter.queue_status()['confirmed'], 1)
        self.assertEqual(self.remove()['status'], 'client-uninstalled')

    def test_conflict_and_unknown_queue_retained(self):
        self.cloud.records[('memory', 'synthetic-memory')] = dict(MEMORY, body='Different remote')
        self.adapter.invoke(self.request())
        self.assertEqual(self.remove()['queue']['conflict'], 1)
        path = self.adapter.cache / 'outbox/unrecognized-item'
        path.write_bytes(b'SYNTHETIC_UNKNOWN')
        with self.assertRaises(ValueError):
            self.remove()
        self.assertEqual(path.read_bytes(), b'SYNTHETIC_UNKNOWN')
        self.assertTrue((self.state / 'codex-mcp.json').exists())

    def test_edited_mapping_or_foreign_skill_file_is_preserved(self):
        path = self.home / '.agents/skills/synthetic-skill/user-notes.txt'
        path.write_text('Local-only user data')
        with self.assertRaises(Exception):
            self.remove()
        self.assertEqual(path.read_text(), 'Local-only user data')
        path.unlink()
        agents = self.native / 'AGENTS.md'
        agents.write_text(agents.read_text() + 'Outside edit\n')
        with self.assertRaises(Exception):
            self.remove()
        self.assertIn('Outside edit', agents.read_text())
        self.assertTrue((self.state / 'codex-mcp.json').exists())

    def test_live_shared_worker_lease_blocks_without_force_or_mutation(self):
        before = self.f.config.read_bytes()
        with locked(self.adapter.cache / '.content-runtime.lock', shared=True):
            with self.assertRaises(RuntimeError):
                self.remove()
        self.assertEqual(self.f.config.read_bytes(), before)
        self.assertFalse(lifecycle.journal_path(self.home).exists())

    def test_linked_lock_never_touches_target_or_native_registration(self):
        target = self.home / 'user-file'
        target.write_bytes(b'')
        lock = self.state / 'content-install.lock'
        lock.unlink()
        lock.symlink_to(target)
        before = self.f.config.read_bytes()
        with self.assertRaises(ValueError):
            self.remove()
        self.assertEqual(target.read_bytes(), b'')
        self.assertTrue(lock.is_symlink())
        self.assertEqual(self.f.config.read_bytes(), before)

    def test_interrupted_removal_repair_and_outside_edit_preservation(self):
        original = install.atomic
        def crash(path, *args):
            original(path, *args)
            if path == self.f.config:
                raise SystemExit('Synthetic interruption')
        with patch.object(install, 'atomic', side_effect=crash):
            with self.assertRaises(SystemExit):
                self.remove()
        self.assertEqual(lifecycle.read_removal(self.home)['phase'], 'applying')
        self.assertEqual(lifecycle.status(self.home)['status'], 'content-native-install-review-required')
        self.assertEqual(lifecycle.repair(self.home)['status'], 'client-uninstalled')
        self.assertFalse((self.state / 'codex-mcp.json').exists())

    def test_interrupted_receipt_does_not_overwrite_outside_edit(self):
        journal = lifecycle.prepare_removal(self.home)
        journal['phase'] = 'applying'
        install.save(lifecycle.journal_path(self.home), journal)
        self.f.config.write_text(self.f.config.read_text() + '\n# Outside edit\n')
        with self.assertRaises(ValueError):
            lifecycle.repair(self.home)
        self.assertIn('# Outside edit', self.f.config.read_text())
        self.assertTrue((self.state / 'codex-mcp.json').exists())

    def test_receipt_cannot_add_scope_or_change_mapping_output(self):
        journal = lifecycle.prepare_removal(self.home)
        name = str(self.home / '.config/agent-brain-client/content/memory/synthetic-memory.md')
        journal['after'][name] = 'U1lOVEhFVElDX1VOUkVWSUVXRURfV1JJVEU='
        with self.assertRaises(ValueError):
            lifecycle.finish_removal(self.home, journal)
        self.assertTrue((self.state / 'codex-mcp.json').exists())

    def test_reinstall_interruption_resumes_saved_plan_without_new_input(self):
        self.remove()
        original = install.atomic
        def crash(path, *args):
            original(path, *args)
            if path == self.f.config:
                raise SystemExit('Synthetic interrupted reinstall')
        with patch.object(install, 'atomic', side_effect=crash):
            with self.assertRaises(SystemExit):
                self.f.run_wizard()
        self.assertTrue((self.preparation / 'reinstall-current.json').exists())
        self.assertTrue(self.f.run_wizard()['installed'])
        self.assertEqual(self.f.prompts, 1)
        self.assertEqual(self.f.backend.record, self.identity)

    def test_legacy_installed_profile_can_reinstall_without_first_install_binding(self):
        self.remove()
        # Model a pre-wizard installation: identity/profile/runtime exist, but
        # no wizard connection/binding or initial plan is required to restore it.
        for name in ('connection.json', 'binding.json', 'install-plan.json', 'pairing-attempt.json'):
            (self.preparation / name).unlink(missing_ok=True)
        self.assertTrue(self.f.run_wizard()['installed'])
        self.assertEqual(self.f.prompts, 1)
        self.assertEqual(self.f.backend.record, self.identity)

    def test_missing_identity_during_reinstall_never_prompts_or_creates_one(self):
        self.remove()
        self.f.backend.record = None
        before = len(self.f.transport.prepares)
        self.assertFalse(self.f.run_wizard()['installed'])
        self.assertEqual(self.f.prompts, 1)
        self.assertIsNone(self.f.backend.record)
        self.assertEqual(len(self.f.transport.prepares), before)

    def test_operator_python_can_differ_for_status_removal_and_receipt_recovery(self):
        other = self.home / "other-python"
        other.write_bytes(b"SYNTHETIC_UNUSED_INTERPRETER")
        other.chmod(0o700)
        identity = copy.deepcopy(self.f.backend.record)
        with (
            patch("sys.executable", str(other)),
            patch.object(
                self.f.backend,
                "load",
                side_effect=AssertionError("Credential access forbidden"),
            ),
        ):
            result = lifecycle.status(self.home)
            self.assertTrue(result["installed"], result)
            self.assertEqual(result["queue"]["pending"], 0)
            self.assertEqual(result["owned_mapping_files"], 4)
            self.assertEqual(self.remove()["status"], "client-uninstalled")
            self.assertEqual(
                lifecycle.status(self.home)["status"], "client-uninstalled"
            )
        self.assertEqual(self.f.backend.record, identity)

    def test_unknown_queue_status_is_present_review_with_action(self):
        path = self.adapter.cache / "outbox"
        path.mkdir(mode=0o700)
        (path / "unknown-item").write_bytes(b"SYNTHETIC_UNKNOWN")
        result = lifecycle.status(self.home)
        self.assertFalse(result["installed"])
        self.assertTrue(result["installation_present"])
        self.assertEqual(result["reason"], "local-content-state-review-required")
        self.assertEqual(result["installed_version"], "0.2.5")
        self.assertIn("Preserve", result["next_action"])

    def test_pinned_dependency_bytes_still_checked_from_other_python(self):
        other = self.home / "other-python"
        other.write_bytes(b"SYNTHETIC_UNUSED_INTERPRETER")
        other.chmod(0o700)
        original = self.home / "tools/python3"
        original.write_bytes(b"SYNTHETIC_DEPENDENCY_CHANGED")
        with patch("sys.executable", str(other)):
            result = lifecycle.status(self.home)
        self.assertFalse(result["installed"])
        self.assertTrue(result["installation_present"])
        self.assertEqual(result["reason"], "policy-profile-bootstrap-review-required")

    def test_host_can_pause_only_this_service_then_owned_uninstall(self):
        metadata = install.private_json(self.state / "codex-mcp.json")
        self.f.config.write_text(
            self.f.config.read_text().replace("enabled = true", "enabled = false")
        )
        result = lifecycle.status(self.home)
        self.assertEqual(result["status"], "content-native-client-disabled")
        self.assertFalse(result["installed"])
        self.assertTrue(result["registration_verified"])
        self.assertEqual(result["owned_mapping_files"], 4)
        self.assertEqual(install.private_json(self.state / "codex-mcp.json"), metadata)
        self.assertEqual(self.remove()["status"], "client-uninstalled")
        self.assertEqual(lifecycle.status(self.home)["status"], "client-uninstalled")
        self.assertEqual(self.f.backend.record, self.identity)

    def test_paused_service_cannot_change_command_or_other_mcp_fields(self):
        self.f.config.write_text(
            self.f.config.read_text()
            .replace("enabled = true", "enabled = false")
            .replace(
                'default_tools_approval_mode = "writes"',
                'default_tools_approval_mode = "auto"',
            )
        )
        before = self.f.config.read_bytes()
        result = lifecycle.status(self.home)
        self.assertFalse(result["installed"])
        self.assertEqual(result["reason"], "mcp-binding-changed")
        with self.assertRaises(ValueError):
            self.remove()
        self.assertEqual(self.f.config.read_bytes(), before)

    def test_edit_after_global_preflight_is_retained_before_each_unlink_and_repair(
        self,
    ):
        target = (
            self.home / ".config/agent-brain-client/content/memory/synthetic-memory.md"
        )
        original = install.atomic

        def edit_after_config(path, *args):
            original(path, *args)
            if path == self.f.config:
                target.write_bytes(b"SYNTHETIC_CONCURRENT_USER_EDIT")

        with patch.object(install, "atomic", side_effect=edit_after_config):
            with self.assertRaises(ValueError):
                self.remove()
        self.assertEqual(target.read_bytes(), b"SYNTHETIC_CONCURRENT_USER_EDIT")
        self.assertEqual(lifecycle.read_removal(self.home)["phase"], "applying")
        with self.assertRaises(ValueError):
            lifecycle.repair(self.home)
        self.assertEqual(target.read_bytes(), b"SYNTHETIC_CONCURRENT_USER_EDIT")

    def test_windows_boundary_precedes_any_local_access(self):
        with patch("sys.platform", "win32"):
            with self.assertRaises(PlatformUnsupported):
                lifecycle.uninstall(self.home, lambda text: 'Continue')
            with self.assertRaises(PlatformUnsupported):
                lifecycle.status(self.home)


if __name__ == '__main__':
    unittest.main()
