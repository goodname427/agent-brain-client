"""Synthetic install/use/remove/reinstall; no real Keychain, server or Codex."""
import copy
import io
import json
import os
from pathlib import Path
import sys
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

    def connection_cli(self, *args, execute=True):
        """Use the real argparse/resolver/transaction with synthetic OS/transport."""
        wizard = fixture.wizard
        original = wizard.run

        def run(home, native, connection, digest):
            return original(home, native, connection, digest,
                            confirm=lambda *a: 'Continue', store=self.f.store,
                            transport=self.f.transport,
                            secure_input=lambda: self.fail('Reinstall must not request an invitation'),
                            dependency_preparer=lambda *a, **k: self.fail('Existing dependencies must be reused'),
                            installer=lambda p, d: install.apply(p, d, lambda value: None))

        output = io.StringIO()
        with patch.object(sys, 'argv', ['mac_first_install.py', *args]), \
                patch.object(Path, 'home', return_value=self.home), \
                patch.dict(os.environ, CODEX_HOME=str(self.native)), \
                patch.object(sys, 'stdout', output), \
                patch.object(wizard, 'run', side_effect=run if execute else AssertionError('Invalid recovery must stop before installation')):
            code = wizard.main()
        return code, json.loads(output.getvalue())

    def test_cli_reinstalls_legacy_identity_without_external_or_saved_connection(self):
        self.remove()
        for name in ('connection.json', 'binding.json', 'install-plan.json', 'pairing-attempt.json'):
            (self.preparation / name).unlink(missing_ok=True)
        (self.f.base / 'connection.json').unlink()
        code, result = self.connection_cli()
        self.assertEqual(code, 0, result)
        self.assertTrue(result['installed'], result)
        self.assertEqual(self.f.prompts, 1)
        self.assertEqual(self.f.backend.record, self.identity)
        self.assertTrue(install.inspect(self.home)['installed'])
        self.assertFalse((self.preparation / 'connection.json').exists())

    def test_cli_uses_saved_connection_only_with_matching_verified_binding(self):
        before = (self.preparation / 'connection.json').read_bytes()
        with patch.object(self.f.backend, 'load', side_effect=AssertionError('Metadata resolution must not access credentials')):
            code, result = self.connection_cli()
        self.assertEqual(code, 0, result)
        self.assertEqual((self.preparation / 'connection.json').read_bytes(), before)
        saved = json.loads(before)
        saved['grant']['actor'] = 'synthetic-other-owner'
        install.save(self.preparation / 'connection.json', saved)
        code, result = self.connection_cli(execute=False)
        self.assertEqual((code, result['status']), (2, 'connection-recovery-review-required'))

    def test_cli_rejects_explicit_missing_file_without_fallback(self):
        self.remove()
        code, result = self.connection_cli('--connection', str(self.home / 'missing.json'), execute=False)
        self.assertEqual((code, result['status']), (2, 'connection-file-review-required'))

    def test_cli_missing_identity_does_not_create_one_or_request_invitation(self):
        self.remove()
        self.f.backend.record = None
        before = len(self.f.transport.prepares)
        code, result = self.connection_cli()
        self.assertEqual(code, 2, result)
        self.assertFalse(result['installed'])
        self.assertIsNone(self.f.backend.record)
        self.assertEqual(len(self.f.transport.prepares), before)
        self.assertEqual(self.f.prompts, 1)

    def test_cli_resumes_interrupted_reinstall_without_external_file_or_new_identity(self):
        self.remove()
        (self.preparation / 'connection.json').unlink()
        (self.f.base / 'connection.json').unlink()
        original = install.atomic

        def crash(path, *args):
            original(path, *args)
            if path == self.f.config:
                raise SystemExit('Synthetic interrupted CLI reinstall')

        with patch.object(install, 'atomic', side_effect=crash):
            with self.assertRaises(SystemExit):
                self.connection_cli()
        code, result = self.connection_cli('--action', 'install')
        self.assertEqual(code, 0, result)
        self.assertTrue(result['installed'], result)
        self.assertEqual(self.f.backend.record, self.identity)
        self.assertEqual(self.f.prompts, 1)

    def test_cli_rejects_another_native_home_or_unfinished_removal(self):
        self.remove()
        with self.assertRaises(ValueError):
            fixture.wizard.resolve_connection(self.home, self.home / 'different-codex')
        journal = lifecycle.read_removal(self.home)
        journal['phase'] = 'applying'
        install.save(lifecycle.journal_path(self.home), journal)
        code, result = self.connection_cli(execute=False)
        self.assertEqual((code, result['status']), (2, 'connection-recovery-review-required'))

    def test_cli_rejects_profile_policy_receipt_or_postimage_drift_before_install(self):
        self.remove()
        receipt = lifecycle.read_removal(self.home)
        policy_path = Path(receipt['registration']['release_policy'])
        policy = install.private_json(policy_path)
        profile_path = Path(policy['runtime']['https_profile'])
        for path in (profile_path, policy_path, lifecycle.journal_path(self.home), self.native / 'config.toml'):
            before = path.read_bytes()
            with self.subTest(file=path.name):
                path.write_bytes(before + b'\nSYNTHETIC_DRIFT')
                with patch.object(self.f.backend, 'load', side_effect=AssertionError('Credential access forbidden')):
                    code, result = self.connection_cli(execute=False)
                self.assertEqual((code, result['status']), (2, 'connection-recovery-review-required'))
                self.assertFalse(result['credential_accessed'])
                self.assertEqual(path.read_bytes(), before + b'\nSYNTHETIC_DRIFT')
            path.write_bytes(before)

    def test_reinstall_keeps_valid_unrelated_config_edits_after_uninstall(self):
        self.remove()
        self.f.config.write_text(
            'model = "synthetic-user-model"\n'
            '[features]\nparallel = true\n'
            '[mcp_servers.other]\ncommand = "preserve-user-server"\n'
        )
        code, result = self.connection_cli()
        self.assertEqual(code, 0, result)
        self.assertTrue(result['installed'], result)
        parsed = install.tomllib.loads(self.f.config.read_text())
        self.assertEqual(parsed['model'], 'synthetic-user-model')
        self.assertTrue(parsed['features']['parallel'])
        self.assertEqual(parsed['mcp_servers']['other']['command'], 'preserve-user-server')
        self.assertIn(install.NAME, parsed['mcp_servers'])
        self.assertEqual(self.f.backend.record, self.identity)

    def test_status_and_reinstall_accept_unrelated_config_changes_without_losing_them(self):
        self.remove()
        edited = (
            'model = "synthetic-status-model"\\n'
            '[features]\\nparallel = true\\n'
            '[mcp_servers.other]\\ncommand = "preserve-after-status"\\n'
        )
        self.f.config.write_text(edited)
        code, status = self.status_cli()
        self.assertEqual(code, 0, status)
        self.assertEqual(status['status'], 'client-uninstalled')
        self.assertTrue(status['uninstall_verified'])
        self.assertEqual(self.f.config.read_text(), edited)

        code, result = self.connection_cli()
        self.assertEqual(code, 0, result)
        self.assertTrue(result['installed'], result)
        parsed = install.tomllib.loads(self.f.config.read_text())
        self.assertEqual(parsed['model'], 'synthetic-status-model')
        self.assertEqual(parsed['mcp_servers']['other']['command'], 'preserve-after-status')
        self.assertIn(install.NAME, parsed['mcp_servers'])

    def test_uninstall_status_and_reinstall_accept_skill_owned_config_toml(self):
        skill = copy.deepcopy(self.cloud.records[('skill', 'synthetic-skill')])
        skill['files']['config.toml'] = 'synthetic skill-local configuration'
        self.cloud.records[('skill', 'synthetic-skill')] = skill
        self.assertEqual(self.adapter.refresh()['status'], 'content-refreshed')
        skill_config = self.home / '.agents/skills/synthetic-skill/config.toml'
        self.assertEqual(skill_config.read_text(), 'synthetic skill-local configuration')

        removed = self.remove()
        self.assertEqual(removed['status'], 'client-uninstalled')
        code, status = self.status_cli()
        self.assertEqual(code, 0, status)
        self.assertTrue(status['uninstall_verified'], status)

        code, result = self.connection_cli()
        self.assertEqual(code, 0, result)
        self.assertTrue(result['installed'], result)
        self.assertTrue(skill_config.exists())
        self.assertEqual(skill_config.read_text(), 'synthetic skill-local configuration')

    def test_reinstall_rejects_invalid_or_conflicting_owned_config_and_owned_file_drift(self):
        cases = (
            ('invalid-toml', 'model = [\n'),
            ('owned-table-conflict',
             'model = "synthetic"\n[mcp_servers.' + install.NAME + ']\ncommand="foreign"\n'),
        )
        for label, text in cases:
            with self.subTest(case=label):
                self.remove()
                self.f.config.write_text(text)
                code, result = self.connection_cli(execute=False)
                self.assertEqual((code, result['status']), (2, 'connection-recovery-review-required'))
                self.assertEqual(self.f.config.read_text(), text)
                # Restore the exact uninstall postimage before starting the next case.
                postimage = lifecycle.decode(lifecycle.read_removal(self.home)['after'][str(self.f.config)])
                self.f.config.write_bytes(postimage)
        self.remove()
        owned = self.native / 'AGENTS.md'
        owned.write_text(owned.read_text() + 'User edit after removal.\n')
        code, result = self.connection_cli(execute=False)
        self.assertEqual((code, result['status']), (2, 'connection-recovery-review-required'))
        self.assertIn('User edit after removal.', owned.read_text())

    def test_interrupted_reinstall_keeps_unrelated_config_preimage_strict(self):
        self.remove()
        self.f.config.write_text('model = "changed-after-uninstall"\n[mcp_servers.other]\ncommand="keep"\n')
        original = install.atomic

        def crash(path, *args):
            original(path, *args)
            if path == self.f.config:
                raise SystemExit('Synthetic interrupted reinstall after valid edit')

        with patch.object(install, 'atomic', side_effect=crash):
            with self.assertRaises(SystemExit):
                self.connection_cli()
        self.assertTrue((self.preparation / 'reinstall-current.json').exists())
        code, result = self.connection_cli()
        self.assertEqual(code, 0, result)
        self.assertTrue(result['installed'], result)
        parsed = install.tomllib.loads(self.f.config.read_text())
        self.assertEqual(parsed['model'], 'changed-after-uninstall')
        self.assertEqual(parsed['mcp_servers']['other']['command'], 'keep')

    def test_cli_rejects_linked_or_public_saved_connection_without_overwrite(self):
        saved = self.preparation / 'connection.json'
        before = saved.read_bytes()
        saved.chmod(0o644)
        code, result = self.connection_cli(execute=False)
        self.assertEqual((code, result['status']), (2, 'connection-recovery-review-required'))
        saved.chmod(0o600)
        saved.unlink()
        saved.symlink_to(self.f.base / 'connection.json')
        code, result = self.connection_cli(execute=False)
        self.assertEqual((code, result['status']), (2, 'connection-recovery-review-required'))
        self.assertEqual(saved.read_bytes(), before)

    def request(self, rid='synthetic_lifecycle_write_0001'):
        return {'request_id': rid, 'method': 'content_put', 'params': {'kind': 'memory', 'id': 'synthetic-memory', 'record': dict(MEMORY, body='Synthetic updated body'), 'expected_revision': install.contract.envelope('memory', 'synthetic-memory', MEMORY)['revision']}}

    def filesystem_state(self):
        result = {}
        for path in sorted(self.home.rglob('*')):
            stat = path.lstat()
            result[str(path)] = (stat.st_mode, stat.st_ino, stat.st_nlink,
                                 stat.st_size, stat.st_mtime_ns,
                                 path.readlink().as_posix() if path.is_symlink()
                                 else path.read_bytes() if path.is_file() else None)
        return result

    def uninstall_cli(self):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['mac_first_install.py', '--action', 'uninstall']), \
                patch.object(Path, 'home', return_value=self.home), \
                patch.dict(os.environ, CODEX_HOME=str(self.native)), \
                patch.object(sys, 'stdout', output), \
                patch.object(fixture.wizard, 'confirm_target', return_value='Continue'):
            code = fixture.wizard.main()
        return code, json.loads(output.getvalue())

    def test_public_cli_supports_two_uninstall_reinstall_cycles(self):
        for cycle in range(2):
            with self.subTest(cycle=cycle + 1):
                code, removed = self.uninstall_cli()
                self.assertEqual(code, 0, removed)
                self.assertEqual(removed['status'], 'client-uninstalled', removed)
                code, verified = self.status_cli()
                self.assertEqual(code, 0, verified)
                self.assertTrue(verified['uninstall_verified'], verified)

                code, installed = self.connection_cli()
                self.assertEqual(code, 0, installed)
                self.assertTrue(installed['installed'], installed)
                self.assertEqual(self.f.backend.record, self.identity)
                self.assertTrue(install.inspect(self.home)['installed'])

    def status_cli(self, action='status'):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['mac_first_install.py', '--action', action]), \
                patch.object(Path, 'home', return_value=self.home), \
                patch.dict(os.environ, CODEX_HOME=str(self.native)), \
                patch.object(sys, 'stdout', output), \
                patch.object(fixture.wizard, 'confirm_target', side_effect=AssertionError('Status/in-use must not open a UI')):
            code = fixture.wizard.main()
        return code, json.loads(output.getvalue())

    def test_completed_removal_status_verifies_postimages_repeatedly_without_side_effects(self):
        self.remove()
        journal = lifecycle.read_removal(self.home)
        before = self.filesystem_state()
        with patch.object(self.f.backend, 'load', side_effect=AssertionError('No credential access')), \
                patch.object(install, 'atomic', side_effect=AssertionError('No filesystem writes')), \
                patch.object(install, 'save', side_effect=AssertionError('No metadata writes')), \
                patch.object(lifecycle, 'lease', side_effect=AssertionError('No lock mutation')):
            for action in ('status', 'status', 'repair'):
                code, result = self.status_cli(action)
                self.assertEqual(code, 0, result)
                self.assertEqual(result['status'], 'client-uninstalled')
                self.assertTrue(result['uninstall_verified'])
                self.assertEqual(result['verified_postimages'], len(journal['after']))
                self.assertFalse(result['native_changed'])
        self.assertEqual(self.filesystem_state(), before)

    def test_completed_removal_status_accepts_unrelated_config_edits(self):
        self.remove()
        journal = lifecycle.read_removal(self.home)
        self.f.config.write_text(
            'model = "synthetic-user-model"\n'
            '[features]\nparallel = true\n'
            '[mcp_servers.other]\ncommand = "preserve-user-server"\n'
        )
        before = self.filesystem_state()
        code, result = self.status_cli()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'client-uninstalled')
        self.assertTrue(result['uninstall_verified'])
        self.assertEqual(result['verified_postimages'], len(journal['after']))
        self.assertEqual(self.filesystem_state(), before)
        parsed = install.tomllib.loads(self.f.config.read_text())
        self.assertEqual(parsed['model'], 'synthetic-user-model')
        self.assertEqual(parsed['mcp_servers']['other']['command'], 'preserve-user-server')

    def test_completed_removal_status_detects_restored_mapping_registration_and_pointers(self):
        self.remove()
        journal = lifecycle.read_removal(self.home)
        targets = [self.native / 'config.toml', self.state / 'codex-mcp.json',
                   self.native / 'AGENTS.md', self.adapter.cache / 'owned.json',
                   self.adapter.cache / 'active.json',
                   self.home / '.config/agent-brain-client/content/memory/synthetic-memory.md',
                   self.home / '.agents/skills/synthetic-skill/SKILL.md']
        for path in targets:
            with self.subTest(path=path.name):
                path.parent.mkdir(parents=True, exist_ok=True)
                old = lifecycle.decode(journal['before'][str(path)]['data'])
                self.assertIsNotNone(old)
                path.write_bytes(old)
                path.chmod(0o600)
                before = self.filesystem_state()
                with patch.object(self.f.backend, 'load', side_effect=AssertionError('No credential access')):
                    for _ in range(2):
                        code, result = self.status_cli()
                        self.assertEqual(code, 2, result)
                        self.assertEqual(result['status'], 'uninstall-state-review-required')
                        self.assertEqual(result['reason'], 'uninstall-postimage-changed')
                        self.assertEqual(result['changed_postimages'], 1)
                        self.assertEqual(result['registration_present'], path == self.state / 'codex-mcp.json')
                        self.assertFalse(result['uninstall_verified'])
                        self.assertFalse(result['native_changed'])
                self.assertEqual(self.filesystem_state(), before)
                desired = lifecycle.decode(journal['after'][str(path)])
                if desired is None:
                    path.unlink()
                else:
                    path.write_bytes(desired)

    def test_completed_removal_status_reports_drift_links_and_bad_receipt_read_only(self):
        self.remove()
        agents = self.native / 'AGENTS.md'
        agents.write_text(agents.read_text() + 'New user edit after uninstall.\n')
        before = self.filesystem_state()
        code, result = self.status_cli()
        self.assertEqual((code, result['reason']), (2, 'uninstall-postimage-changed'))
        self.assertEqual(self.filesystem_state(), before)
        agents.write_bytes(lifecycle.decode(lifecycle.read_removal(self.home)['after'][str(agents)]))
        registration = self.state / 'codex-mcp.json'
        registration.symlink_to(self.home / 'missing-registration-target')
        before = self.filesystem_state()
        code, result = self.status_cli()
        # The receipt's scope check rejects a linked current registration first.
        self.assertEqual((code, result['reason']), (2, 'uninstall-receipt-unverified'))
        self.assertTrue(result['registration_present'])
        self.assertEqual(self.filesystem_state(), before)
        registration.unlink()
        receipt = lifecycle.journal_path(self.home)
        receipt.write_bytes(b'[]')
        before = self.filesystem_state()
        code, result = self.status_cli()
        self.assertEqual((code, result['reason']), (2, 'uninstall-receipt-unverified'))
        self.assertEqual(self.filesystem_state(), before)

    def test_cli_in_use_guides_only_service_pause_reload_without_mutation(self):
        with locked(self.adapter.cache / '.content-runtime.lock', shared=True):
            before = self.filesystem_state()
            code, result = self.status_cli('uninstall')
            self.assertEqual((code, result['status']), (2, 'client-in-use'))
            self.assertIn('only agent_brain_server', result['next_action'])
            self.assertIn('Settings > MCP servers', result['next_action'])
            self.assertIn('Restart/reload', result['next_action'])
            self.assertIn('stop and report', result['next_action'])
            self.assertNotIn('Quit Codex', result['next_action'])
            self.assertFalse(result['native_changed'])
            self.assertEqual(self.filesystem_state(), before)

    def test_install_reinstall_and_repeat_cli_require_separate_onboard_coverage_check(self):
        before_identity = copy.deepcopy(self.f.backend.record)
        prepare_count = len(self.f.transport.prepares)
        for _ in range(2):
            code, result = self.connection_cli()
            self.assertEqual(code, 0, result)
            self.assertEqual(result['model_loaded'], 'unknown')
            handoff = result['onboard_check']
            self.assertEqual(handoff['status'], 'coverage-check-required')
            self.assertEqual(handoff['coverage'], 'unknown')
            self.assertFalse(handoff['account_complete'])
            self.assertFalse(handoff['source_scan_performed'])
            self.assertFalse(handoff['data_transferred'])
        self.remove()
        (self.preparation / 'connection.json').unlink()
        code, result = self.connection_cli()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['onboard_check']['status'], 'coverage-check-required')
        self.assertEqual(self.f.backend.record, before_identity)
        self.assertEqual(len(self.f.transport.prepares), prepare_count)
        self.assertEqual(self.f.prompts, 1)
        self.assertFalse((self.adapter.cache / 'imports.json').exists())

    def test_status_does_not_infer_model_loading_from_registration_or_worker_lease(self):
        with locked(self.adapter.cache / '.content-runtime.lock', shared=True):
            with patch.object(self.f.backend, 'load', side_effect=AssertionError('No credential access')):
                code, result = self.status_cli()
        self.assertEqual(code, 0, result)
        self.assertTrue(result['registration_verified'])
        self.assertEqual(result['model_loaded'], 'unknown')
        self.assertNotIn('onboard_check', result)

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
