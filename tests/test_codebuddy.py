from agent_brain_client.adapters.storage import metadata as metadata_io, registration as registration_store
from agent_brain_client.runtime import host_binding
import agent_brain_client.runtime.host_binding as host_binding
import agent_brain_client.adapters.storage.bounded_json as adapters_storage_bounded_json
import agent_brain_client.adapters.harnesses.hook_output as adapters_harnesses_hook_output
import agent_brain_client.application.memory.context as memory_context
import agent_brain_client.adapters.storage.collection as source_policy
import agent_brain_client.adapters.storage.metadata as metadata
"""CodeBuddy host transactions and real Client outbox with synthetic sources."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / p) for p in ('src', 'src', 'tests')]
import agent_brain_client.adapters.harnesses.codebuddy.configuration as cbhost
import agent_brain_client.adapters.harnesses.codebuddy.events as cb
import agent_brain_client.adapters.harnesses.codebuddy.transcript as native_transcript
import agent_brain_client.interfaces.cli.codebuddy as entry
import agent_brain_client.runtime.collection as gm
from agent_brain_client.domain.scopes import digest
import test_content_outbox as fixture
from test_event_hook import accepted


class Configuration(unittest.TestCase):
    def test_system_python_bootstrap_loads_installer_and_hands_off(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root / 'bootstrap').mkdir()
            script = root / 'bootstrap/codebuddy.py'
            script.write_bytes((ROOT / 'bootstrap/codebuddy.py').read_bytes())
            (root / 'bootstrap/python_runtime.py').write_text(
                'import sys\nfrom pathlib import Path\n'
                'def runtime(home, prepare):\n'
                '    assert prepare is False\n'
                '    return Path(sys.executable)\n')
            code = '''import json, os, runpy, sys
entry = sys.argv[1]
sys.argv = [entry, 'status']
sys.version_info = (3, 9)
def handoff(path, argv):
    print(json.dumps({'path': path, 'argv': argv}))
    raise SystemExit(0)
os.execv = handoff
runpy.run_path(entry, run_name='__main__')
'''
            result = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(script)],
                                    capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            argv = json.loads(result.stdout)['argv']
            self.assertTrue(os.path.samefile(argv[0], sys.executable))
            self.assertEqual(argv[1:], ['-X', 'utf8', '-I', '-B', str(root / 'src/agent_brain_client/interfaces/cli/codebuddy.py'), 'status'])

    def test_jsonc_narrow_edit_keeps_comments_urls_and_other_servers(self):
        text = '{\n// user settings\n"permissions":{"allow":["Read"],},\n"mcpServers":{ /* keep */ "other":{"url":"https://example.invalid/a//b"},},\n}\n'
        changed = cbhost.render_json(text, {'command': 'python', 'args': ['a b']}, None)
        self.assertIn('// user settings', changed.decode())
        self.assertIn('/* keep */ "other":{"url":"https://example.invalid/a//b"}', changed.decode())
        self.assertEqual(cbhost.Document(changed.decode()).value['permissions'], {'allow': ['Read']})
        removed = cbhost.render_json(changed.decode(), None, {'command': 'python', 'args': ['a b']}, remove=True)
        self.assertEqual(cbhost.Document(removed.decode()).value, cbhost.Document(text).value)

    def test_invalid_and_duplicate_config_preserved(self):
        for text in ('{"hooks":{},"hooks":{}}', '{"a":NaN}', '{"a":1', '[]', '{/* unclosed'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                cbhost.Document(text)
        with self.assertRaises(ValueError):
            cbhost.render_json('{"mcpServers":{"agent_brain_server":{"command":"user-owned"}}}', {}, None)

    def test_hook_merge_keeps_permission_and_other_hook_semantics(self):
        existing = {'hooks': {'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': 'user-hook'}]}], 'Stop': [{'hooks': []}]}, 'permissions': {'deny': ['Bash']}}
        wanted = {kind: {'hooks': [{'type': 'command', 'command': 'adapter-hook'}]} for kind in ('UserPromptSubmit', 'SessionStart', 'Stop')}
        first = cbhost.render_json(json.dumps(existing), wanted, None, hooks=True)
        second = cbhost.render_json(first.decode(), wanted, wanted, hooks=True)
        self.assertEqual(first, second)
        result = cbhost.Document(second.decode()).value
        self.assertEqual(result['permissions'], existing['permissions'])
        self.assertEqual(result['hooks']['Stop'][0], existing['hooks']['Stop'][0])
        self.assertEqual(result['hooks']['UserPromptSubmit'][0], existing['hooks']['UserPromptSubmit'][0])
        with self.assertRaises(ValueError):
            cbhost.render_json(first.decode().replace('adapter-hook', 'outside-edit'), wanted, wanted, hooks=True)

    def test_cli_priority_ide_path_and_guidance_ownership(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            native = home / '.codebuddy'
            native.mkdir()
            self.assertEqual(cbhost.mcp_paths(home, 'both'), [native / 'mcp.json', native / '.mcp.json'])
            (native / 'mcp.json').write_text('{}')
            self.assertEqual(cbhost.mcp_paths(home, 'both'), [native / 'mcp.json'])
            (native / '.mcp.json').write_text('{}')
            self.assertEqual(cbhost.mcp_paths(home, 'cli'), [native / '.mcp.json'])
        source = 'Existing personal instruction\n'
        mapped = cbhost.guidance(source)
        self.assertIn(source, mapped.decode())
        self.assertNotIn(cbhost.BEGIN, cbhost.guidance(mapped.decode(), previous=True, remove=True).decode())
        with self.assertRaises(ValueError):
            cbhost.guidance(mapped.decode())


@unittest.skipUnless(sys.platform in ('darwin', 'win32'), 'Native Client filesystem required')
class Installation(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve() / 'home'
        self.home.mkdir()
        self.native = self.home / '.codebuddy'
        self.native.mkdir()
        self.settings = self.native / 'settings.json'
        self.original = b'{// keep comment\n"permissions":{"deny":["Bash"]},"hooks":{"Stop":[]}}\n'
        self.settings.write_bytes(self.original)
        self.core = SimpleNamespace()

    def install(self, **kwargs):
        return cbhost.install(self.home, ROOT, self.core, **kwargs)

    def test_install_repeat_uninstall_and_frozen_code_survive_source_checkout(self):
        if os.name == 'nt':
            from test_windows_native import host_security
            host_before=host_security(self.settings)
        result = self.install()
        self.assertTrue(result['installed'])
        from agent_brain_client.adapters.platforms.filesystem import private_mode
        private_mode(cbhost.state_directory(self.home)/'registration.json',owner=True)
        if os.name == 'nt':
            self.assertEqual(host_security(self.settings),host_before)
        self.assertFalse(result['model_loaded'])
        settings = cbhost.Document(self.settings.read_text()).value
        self.assertEqual(settings['permissions'], {'deny': ['Bash']})
        self.assertEqual(len(settings['hooks']['Stop']), 1)
        self.assertIn('// keep comment', self.settings.read_text())
        after = self.settings.read_bytes()
        self.install()
        self.assertEqual(after, self.settings.read_bytes())
        receipt = adapters_storage_bounded_json.read_json(cbhost.state_directory(self.home) / 'registration.json')
        command = receipt['mcp']
        entry_path = Path(command['args'][command['args'].index('-B') + 1])
        self.assertNotIn(str(ROOT), str(entry_path))
        self.assertEqual(entry_path.read_bytes(), (ROOT / 'src/agent_brain_client/interfaces/cli/codebuddy.py').read_bytes())
        isolated = subprocess.run([sys.executable, '-I', '-B', str(entry_path), 'status', '--device-home', str(self.home)], cwd=self.home, capture_output=True, timeout=15)
        self.assertEqual(isolated.returncode, 2)
        self.assertEqual(json.loads(isolated.stdout)['status'], 'codebuddy-review-or-resume-required')
        self.assertNotIn(b'ModuleNotFoundError', isolated.stderr)
        self.assertTrue(self.install(remove=True)['backend_preserved'])
        settings = cbhost.Document(self.settings.read_text()).value
        self.assertEqual(settings['permissions'], {'deny': ['Bash']})
        self.assertEqual(settings['hooks']['UserPromptSubmit'], [])
        self.assertNotIn('agent_brain_server', (self.native / '.mcp.json').read_text())
        self.assertFalse((self.home / '.codex').exists())
        self.install()

    def test_shared_runtime_drift_requires_binding_refresh_before_loading_core(self):
        from agent_brain_client.adapters.packages.layout import PYTHON_FILES
        from agent_brain_client.adapters.platforms.filesystem import private_directory
        from agent_brain_client.adapters.harnesses.codex.collection_installation import freeze_code
        directory = private_directory(source_policy.settings_path(self.home).parent)
        pins = {name: metadata.digest((ROOT / name).read_bytes()) for name in PYTHON_FILES}
        freeze_code(directory, pins)
        metadata.write(directory / 'settings.json', {'device_home':str(self.home), 'code_sha256':pins})
        self.assertIs(host_binding.load_core(self.home), gm)
        changed = dict(pins)
        name = 'src/agent_brain_client/runtime/collection.py'
        raw = (ROOT / name).read_bytes() + b'\n# Synthetic different reviewed runtime\n'
        changed[name] = metadata.digest(raw)
        root = private_directory(directory / 'code' / metadata.digest(json.dumps(changed, sort_keys=True).encode()))
        for file in PYTHON_FILES:
            target = root / file
            private_directory(target.parent)
            target.write_bytes(raw if file == name else (ROOT / file).read_bytes())
            target.chmod(0o600)
        metadata.write(directory / 'settings.json', {'device_home':str(self.home), 'code_sha256':changed})
        with self.assertRaisesRegex(ValueError, 'Refresh the Harness binding'):
            host_binding.load_core(self.home)

    def test_existing_binding_or_edited_owned_hook_is_not_overwritten(self):
        config = self.native / 'mcp.json'
        config.write_text('{"mcpServers":{"agent_brain_server":{"command":"other"}}}')
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.settings.read_bytes(), self.original)
        config.unlink()
        self.install()
        original = self.settings.read_text()
        text = cbhost.Document(original).patch(('hooks', 'Stop', 0, 'hooks', 0, 'command'),
                                               'synthetic-outside-edit')
        self.assertNotEqual(text, original)
        self.settings.write_text(text)
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.settings.read_text(), text)

    def test_ide_rule_migrates_legacy_receipt_and_preserves_user_rules(self):
        self.install(surface='ide')
        rule = self.native / 'rules/agent-brain-client.md'
        self.assertIn(cbhost.GUIDANCE, rule.read_text())
        self.assertFalse((self.native / 'CODEBUDDY.md').exists())
        user_rule = self.native / 'rules/agent-brain.md'
        user_rule.write_text('---\nenabled: false\n---\nKeep my personal rule unchanged.\n')
        legacy = self.native / 'CODEBUDDY.md'
        legacy.write_text('Personal instruction\n\n' + cbhost.GUIDANCE + '\n')
        rule.unlink()
        receipt_file = cbhost.state_directory(self.home) / 'registration.json'
        receipt = metadata.private_json(receipt_file)
        receipt.pop('instruction_paths')
        metadata.write(receipt_file, receipt)
        self.assertTrue(cbhost.refresh(self.home, ROOT, check_only=True)['update_available'])
        self.assertFalse(rule.exists())
        self.assertTrue(cbhost.refresh(self.home, ROOT)['native_changed'])
        self.assertIn(cbhost.GUIDANCE, rule.read_text())
        self.assertTrue(rule.read_text().startswith(cbhost.IDE_HEADER))
        self.assertNotIn(cbhost.BEGIN, legacy.read_text())
        self.assertIn('Personal instruction', legacy.read_text())
        self.assertEqual(user_rule.read_text(), '---\nenabled: false\n---\nKeep my personal rule unchanged.\n')
        self.install(surface='ide', remove=True)
        self.assertNotIn(cbhost.BEGIN, rule.read_text())
        self.assertEqual(user_rule.read_text(), '---\nenabled: false\n---\nKeep my personal rule unchanged.\n')

    def test_ide_rule_is_automatic_on_creation_and_keeps_user_disabled_header(self):
        self.install(surface='both')
        rule = self.native / 'rules/agent-brain-client.md'
        self.assertTrue(rule.read_text().startswith('---\nalwaysApply: true\nenabled: true\n---\n'))
        self.assertFalse((self.native / 'CODEBUDDY.md').read_text().startswith('---'))
        disabled = rule.read_text().replace('enabled: true', 'enabled: false').replace(
            'alwaysApply: true', 'alwaysApply: false\ndescription: User controlled')
        rule.write_text(disabled)
        cbhost.refresh(self.home, ROOT)
        self.assertEqual(rule.read_text(), disabled)
        self.install(remove=True)
        self.assertIn('enabled: false', rule.read_text())
        self.assertIn('alwaysApply: false', rule.read_text())
        self.assertNotIn(cbhost.BEGIN, rule.read_text())

    def test_upgrade_refreshes_existing_surface_without_installing_absent_hosts(self):
        from agent_brain_client.adapters.harnesses.codex import updates
        from agent_brain_client.adapters.packages.layout import PYTHON_FILES
        adapter = updates.CodexUpdateInstallation(self.home, setup_sources=lambda: {},
            refresh_bindings=lambda package, **kw: cbhost.refresh(
                self.home, package, check_only=kw["check_only"]))
        active = {"package": str(ROOT), "version": "0.8.5", "commit": "c" * 40}
        adapter.channel = SimpleNamespace(verified_active=lambda: active)
        def shared_refresh(**kwargs):
            return {'status': 'global-hook-current', 'native_changed': False,
                    'update_available': False}
        with patch.object(updates.hooks, 'refresh', side_effect=lambda *a, **kw: shared_refresh(**kw)):
            adapter.refresh_hooks()
            self.assertFalse((cbhost.state_directory(self.home) / 'registration.json').exists())
            self.install(surface='cli')
            old = (self.native / '.mcp.json').read_bytes()
            source = self.home / 'synthetic-source'
            for name in PYTHON_FILES:
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes())
            changed = source / 'src/agent_brain_client/runtime/collection.py'
            changed.write_bytes(changed.read_bytes() + b'\n# synthetic upgrade\n')
            active["package"] = str(source)
            with patch.object(updates.hooks, 'ROOT', source):
                checked = adapter.refresh_hooks(check_only=True)
                self.assertTrue(checked['update_available'])
                self.assertEqual((self.native / '.mcp.json').read_bytes(), old)
                updated = adapter.refresh_hooks()
                self.assertTrue(updated['native_changed'])
                self.assertNotEqual((self.native / '.mcp.json').read_bytes(), old)
                self.assertFalse((self.native / 'mcp.json').exists())
                self.assertFalse((self.native / 'rules/agent-brain-client.md').exists())
                receipt = metadata.private_json(cbhost.state_directory(self.home) / 'registration.json')
                frozen = Path(receipt['mcp']['args'][4]).parents[4]
                self.assertEqual((frozen / 'src/agent_brain_client/runtime/collection.py').read_bytes(), changed.read_bytes())
                self.assertFalse(adapter.refresh_hooks()['native_changed'])
            self.install(surface='cli', remove=True)
            self.assertFalse(adapter.refresh_hooks()['native_changed'])
            self.assertNotIn(cbhost.NAME, (self.native / '.mcp.json').read_text())

    def test_fresh_installation_removes_only_registered_instruction_blocks(self):
        self.install(surface='both')
        rule = self.native / 'rules/agent-brain-client.md'
        legacy = self.native / 'CODEBUDDY.md'
        user_rule = self.native / 'rules/personal.md'
        user_rule.write_text('Personal instruction remains.\n')
        payload = cbhost.fresh_installation_payload(self.home)
        self.assertNotIn(cbhost.BEGIN.encode(), payload[rule])
        self.assertEqual(payload[rule], b"")
        self.assertNotIn(cbhost.BEGIN.encode(), payload[legacy])
        self.assertNotIn(user_rule, payload)
        self.assertIn(cbhost.BEGIN, rule.read_text())

    def test_rule_collision_and_disabled_binding_are_not_overwritten(self):
        rules = self.native / 'rules'
        rules.mkdir()
        rule = rules / 'agent-brain-client.md'
        rule.write_text(cbhost.GUIDANCE + '\n')
        with self.assertRaises(ValueError):
            self.install(surface='ide')
        self.assertEqual(self.settings.read_bytes(), self.original)
        rule.unlink()
        self.install(surface='ide')
        config = self.native / 'mcp.json'
        disabled = cbhost.Document(config.read_text()).patch(('mcpServers', cbhost.NAME, 'disabled'), True)
        config.write_text(disabled)
        with self.assertRaises(ValueError):
            cbhost.refresh(self.home, ROOT)
        self.assertEqual(config.read_text(), disabled)

    def test_legacy_receipt_owner_repair_requires_the_original_private_journal(self):
        self.install()
        directory = cbhost.state_directory(self.home)
        file, journal = directory / 'registration.json', directory / 'transaction.json'
        record = metadata.private_json(journal)
        record['host'][str(file)].pop('private')
        metadata.write(journal, record)
        before = file.read_bytes()
        settings = self.settings.read_bytes()
        actual = metadata.private_json
        failed = []
        def inherited_owner(path, *args, **kwargs):
            if Path(path) == file and not failed:
                failed.append(True)
                raise ValueError('Owned private Windows item required')
            return actual(path, *args, **kwargs)
        with patch.object(cbhost, 'os', SimpleNamespace(name='nt')), \
                patch.object(metadata_io, 'private_json', side_effect=inherited_owner):
            self.assertEqual(cbhost.registration(file, journal, self.core), json.loads(before))
        self.assertEqual(file.read_bytes(), before)
        self.assertEqual(self.settings.read_bytes(), settings)
        from agent_brain_client.adapters.platforms.filesystem import private_mode
        private_mode(file, owner=True)
        # A changed receipt cannot use the old owner failure to overwrite data.
        file.write_bytes(before + b' ')
        failed.clear()
        with patch.object(cbhost, 'os', SimpleNamespace(name='nt')), \
                patch.object(metadata_io, 'private_json', side_effect=inherited_owner), \
                patch('agent_brain_client.adapters.storage.host_files.host_atomic') as write:
            with self.assertRaises(ValueError):
                cbhost.registration(file, journal, self.core)
            write.assert_not_called()
        self.assertEqual(file.read_bytes(), before + b' ')

    def test_failure_and_interrupted_transaction_restore_all_images(self):
        from agent_brain_client.adapters.storage.host_files import host_apply as real_apply
        def interrupted(rows, persist):
            real_apply(rows, persist)
            raise OSError('Synthetic post-write failure')
        with patch('agent_brain_client.adapters.storage.host_files.host_apply', side_effect=interrupted), self.assertRaises(OSError):
            self.install()
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertFalse((self.native / '.mcp.json').exists())
        self.assertFalse((cbhost.state_directory(self.home) / 'registration.json').exists())
        with patch('agent_brain_client.adapters.storage.host_files.host_apply', side_effect=lambda rows, persist: (real_apply(rows, persist), (_ for _ in ()).throw(SystemExit(7)))):
            with self.assertRaises(SystemExit):
                self.install()
        cbhost.recover(self.home, self.core)
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertFalse((cbhost.state_directory(self.home) / 'registration.json').exists())
        self.install()

    def test_linked_native_config_is_rejected(self):
        other = self.home / 'user.json'
        other.write_text('{}')
        try:
            (self.native / 'mcp.json').symlink_to(other)
        except OSError:
            self.skipTest('Symlink creation unavailable for this account')
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(other.read_text(), '{}')


@unittest.skipUnless(sys.platform in ('darwin', 'win32'), 'Native Client filesystem required')
class Hooks(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.Outbox('test_same_id_cannot_change_request_or_cas_base')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.queue = self.fixture.adapter
        root = str(self.fixture.home)
        self.local = {'kind': 'project', 'cwd': root, 'project_root': root, 'repository_root': root}
        scope = {'schema': 1, 'profile_id': 'a' * 32, 'all_projects': True, 'include_roots': [], 'exclude_roots': [], 'non_project_roots': [], 'recall_personal': True}
        self.value = {'device_home': root, 'enabled': True, 'scope': scope, 'approved_scope_sha256': digest(scope), 'server_scope_sha256': digest(scope)}
        self.client = SimpleNamespace(actor='synthetic-owner', status=lambda session: {'source_admitted': True})
        self.sent = []
        def send(request):
            self.sent.append(copy.deepcopy(request))
            return accepted(request)
        self.fixture.cloud.request = send
        self.core = SimpleNamespace(approved=source_policy.approved, load=lambda home: self.value,
                                    adapter=lambda value, local: (self.client, self.queue),
                                    eligible=lambda value, cwd: self.local, recall=Mock(return_value=[{'content': 'Synthetic recalled memory'}]), hook_context=adapters_harnesses_hook_output.hook_context)
        queue = patch.object(cb, 'open_queue', return_value=(self.client, self.queue))
        queue.start()
        self.addCleanup(queue.stop)
        self.payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'native-session', 'generation_id': 'generation-1',
                        'cwd': root, 'prompt': 'Synthetic user prefers Python', 'transcript_path': '/must-not-be-read/synthetic.jsonl'}

    def hook(self, **changes):
        return cb.hook(self.fixture.home, io.BytesIO(json.dumps(dict(self.payload, **changes)).encode()), core=self.core)

    def test_prompt_capture_real_outbox_deduplicates_and_recalls(self):
        output, state = self.hook()
        self.assertEqual(state['local_queue']['state'], 'confirmed')
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]['params']['event']['messages'], [{'role': 'user', 'content': self.payload['prompt']}])
        self.assertTrue(self.sent[0]['params']['event']['session_id'].startswith('codebuddy-'))
        self.assertIn('Synthetic recalled memory', output['hookSpecificOutput']['additionalContext'])
        self.hook()
        self.assertEqual(len(self.sent), 1)
        self.hook(generation_id='generation-2')
        self.assertEqual(len(self.sent), 2)
        data = '\n'.join(p.read_text() for p in (self.queue.cache / 'outbox').glob('*.json'))
        self.assertNotIn('transcript_path', data)
        self.assertNotIn('/must-not-be-read', data)
        self.assertFalse(state['persistence']['durable'])

    def test_no_generation_does_not_deduplicate_identical_real_prompts(self):
        self.payload.pop('generation_id')
        self.hook()
        self.hook()
        self.assertEqual(len(self.sent), 2)

    def test_offline_queue_retries_exact_payload_on_redelivery(self):
        with patch.object(self.fixture.cloud, 'request', side_effect=OSError('offline')):
            output, state = self.hook()
        self.assertEqual(state['local_queue']['state'], 'pending')
        pending = next((self.queue.cache / 'outbox').glob('*.json')).read_bytes()
        self.assertTrue(output)
        self.hook()
        self.assertEqual(self.sent[0], json.loads(pending)['request'])

    def test_stop_gate_explicit_private_payload_and_capture_failure_keep_recall_independent(self):
        self.value['enabled'] = False
        stream = Mock()
        stream.read.side_effect = AssertionError('Disabled source cannot consume a prompt')
        output, state = cb.hook(self.fixture.home, stream, core=self.core)
        self.assertEqual(state['status'], 'source-approval-required')
        self.value['enabled'] = True
        output, state = self.hook(prompt='LocalOnly: synthetic private content')
        self.assertEqual(output, {})
        self.assertEqual(self.sent, [])
        with patch.object(cb, 'capture', side_effect=PermissionError('SECRET')):
            output, state = self.hook()
        self.assertTrue(output)
        self.assertEqual(state['status'], 'capture-unconfirmed')
        self.assertNotIn('SECRET', json.dumps(state))

    def test_pending_events_replay_on_a_later_hook_with_original_payload(self):
        with patch.object(self.fixture.cloud, 'request', side_effect=OSError('offline')):
            _, state = self.hook()
        self.assertEqual(state['local_queue']['state'], 'pending')
        row = json.loads(next((self.queue.cache / 'outbox').glob('*.json')).read_bytes())
        self.hook(hook_event_name='SessionStart')
        self.assertEqual(self.sent, [row['request']])

    def test_long_unicode_text_is_queued_before_network_and_not_truncated(self):
        text = '合成文字🙂' * 18000
        original = self.fixture.cloud.request
        checked = []
        def send(request):
            if not checked:
                pending = list((self.queue.cache / 'outbox').glob('*.json'))
                self.assertGreater(len(pending), 1)
                checked.append(True)
            return original(request)
        with patch.object(self.fixture.cloud, 'request', side_effect=send):
            result = cb.capture_messages(self.core, self.value, self.local, self.payload,
                                         [{'role': 'assistant', 'content': text}], 'assistant')
        self.assertEqual(result['local_queue']['state'], 'confirmed')
        self.assertEqual(''.join(m['content'] for r in self.sent for m in r['params']['event']['messages']), text)
        for request in self.sent:
            self.assertLessEqual(len(json.dumps(request, ensure_ascii=False).encode()), 120000)

    def test_no_stop_transcript_reader_and_generation_drift_is_rejected(self):
        self.hook()
        with self.assertRaises(ValueError):
            cb.capture(self.core, self.value, self.local, dict(self.payload, prompt='Changed native generation'), phase='prompt-0')
        with self.assertRaises(ValueError):
            self.hook(hook_event_name='Stop', stop_hook_active=False)
        self.assertEqual(len(self.sent), 1)


class Protocol(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin', 'Mac wrapper fixture')
    def test_existing_backend_is_upgraded_before_codebuddy_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            tools = root / 'bootstrap/macos'
            tools.mkdir(parents=True)
            home = root / 'home'
            home.mkdir()
            log = root / 'operations'
            script = tools / 'codebuddy.sh'
            script.write_bytes((ROOT / 'bootstrap/macos/codebuddy.sh').read_bytes())
            (root / 'bootstrap/codebuddy.py').write_text('import os,sys\nfrom pathlib import Path\nif sys.argv[1]=="backend-home": print(str(Path.home()/"shared-native"))\nelse: Path(os.environ["SYNTHETIC_LOG"]).open("a").write("bind\\n")\n')
            (tools / 'install.sh').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SYNTHETIC_LOG"\n')
            env = dict(os.environ, HOME=str(home), SYNTHETIC_LOG=str(log))
            fresh = subprocess.run(['sh', str(script), '--server', 'https://synthetic.invalid'], env=env, capture_output=True)
            self.assertEqual(fresh.returncode, 0, fresh.stderr)
            self.assertEqual(log.read_text().splitlines(), ['--server https://synthetic.invalid', 'bind'])
            log.write_text('')
            registration = home / '.config/agent-brain-client/codex-mcp.json'
            registration.parent.mkdir(parents=True)
            registration.write_text('{}')
            existing = subprocess.run(['sh', str(script)], env=env, capture_output=True)
            self.assertEqual(existing.returncode, 0, existing.stderr)
            self.assertEqual(log.read_text().splitlines(), ['', '--action check-update', '--action update', 'bind'])

    def test_capture_queue_uses_shared_core_and_separate_host_binding(self):
        from agent_brain_client.application.queue.outbox import DurableOutbox
        source = fixture.Outbox('test_same_id_cannot_change_request_or_cas_base')
        source.setUp()
        self.addCleanup(source.doCleanups)
        file = source.home / 'content-policy.json'
        metadata.write(file, fixture.POLICY)
        source.cloud.queue_identity = {'transport': 'synthetic-shared-https'}
        core = SimpleNamespace(CollectionClient=lambda value, local: source.cloud)
        registered = {'runtime': {'content_policy': str(file), 'content_policy_sha256': adapters_storage_bounded_json.digest(file.read_bytes()), 'codex_home': str(source.home / '.codex')}}
        local = {'kind': 'non-project', 'cwd': str(source.home), 'project_root': None, 'repository_root': None}
        with patch.object(registration_store, 'registration', return_value=({}, registered)):
            client, queue = cb.open_queue(core, {'device_home': str(source.home)}, local)
        self.assertIsInstance(queue, DurableOutbox)
        self.assertEqual(client.queue_identity['host_adapter'], 'codebuddy')
        self.assertIn('codebuddy/outboxes', queue.cache.as_posix())
        self.assertNotEqual(queue.queue_sha, source.adapter.queue_sha)
        self.assertFalse((source.home / '.codex').exists())

    def test_gateway_preserves_backend_argv_and_environment(self):
        command = {'command': '/synthetic/python', 'args': ['-I', '/synthetic/startup.py'], 'env': {'SYNTHETIC_CLIENT': '1'}}
        execute = Mock()
        with patch.object(host_binding, 'backend', return_value=(command, {})), patch.object(host_binding.sys, 'platform', 'darwin'):
            host_binding.execute_mcp('/synthetic/home', object(), execute)
        self.assertEqual(execute.call_args.args[:2], ('/synthetic/python', ['/synthetic/python', '-I', '/synthetic/startup.py']))
        self.assertEqual(execute.call_args.args[2]['SYNTHETIC_CLIENT'], '1')

    def test_probe_requires_authenticated_protocol_result(self):
        command = {'command': '/synthetic/python', 'args': ['-I', '/synthetic/startup.py']}
        with patch.object(host_binding, 'backend', return_value=(command, {})):
            for response in ({'installed': True}, {'protocol_checked': True}, {'protocol_checked': True, 'authentication_checked': True}):
                runner = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response).encode()))
                if response.get('authentication_checked'):
                    self.assertTrue(entry.probe('/synthetic/home', object(), runner)['protocol_checked'])
                else:
                    with self.assertRaises(ValueError):
                        entry.probe('/synthetic/home', object(), runner)

    def test_fresh_install_uses_shared_backend_without_codex_home(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            self.assertEqual(entry.backend_home(home), home / '.config/agent-brain-client/shared-native')
            state = home / '.config/agent-brain-client'
            state.mkdir(parents=True)
            policy = state / 'policy.json'
            native = home / 'original-native'
            policy.write_text(json.dumps({'runtime': {'codex_home': str(native)}}))
            (state / 'codex-mcp.json').write_text(json.dumps({'release_policy': str(policy), 'release_policy_sha256': adapters_storage_bounded_json.digest(policy.read_bytes())}))
            self.assertEqual(entry.backend_home(home), native)
            self.assertFalse((home / '.codex').exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'Mac first-install fixture')
    def test_fresh_shared_install_transaction_and_codebuddy_binding(self):
        import test_mac_first_install as first
        fixture = first.FirstInstall('test_first_install_prepared_environment_and_exact_native_transaction')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fixture()
        fixture.native = fixture.home / '.config/agent-brain-client/shared-native'
        fixture.native.mkdir()
        fixture.config = fixture.native / 'config.toml'
        result = fixture.run_wizard()
        self.assertTrue(result['installed'], result)
        metadata = adapters_storage_bounded_json.read_json(fixture.home / '.config/agent-brain-client/codex-mcp.json')
        policy = adapters_storage_bounded_json.read_json(metadata['release_policy'])
        self.assertEqual(policy['runtime']['codex_home'], str(fixture.native))
        self.assertFalse((fixture.home / '.codex').exists())
        bound = cbhost.install(fixture.home, ROOT, SimpleNamespace())
        self.assertTrue(bound['installed'])
        self.assertTrue((fixture.home / '.codebuddy/mcp.json').exists())
        self.assertFalse((fixture.home / '.codex').exists())


@unittest.skipUnless(sys.platform in ('darwin', 'win32'), 'Native Client filesystem required')
class Transcript(Hooks):
    def test_cli_direct_stop_text_uses_observed_turn_without_history_read(self):
        self.hook()
        output, result = self.hook(hook_event_name='Stop', stop_hook_active=False,
                                   last_assistant_message='Actual CLI final reply')
        self.assertEqual(output, {})
        self.assertEqual(result['local_queue']['state'], 'confirmed')
        self.assertEqual(self.sent[-1]['params']['event']['messages'][0]['content'], 'Actual CLI final reply')

    def native_file(self, suffix='.jsonl'):
        directory = self.fixture.home / '.codebuddy/projects/synthetic'
        directory.mkdir(parents=True, exist_ok=True)
        if suffix == '.jsonl':
            path = directory / (self.payload['session_id'] + suffix)
        else:
            directory = directory / self.payload['session_id']
            directory.mkdir()
            path = directory / 'index.json'
        self.payload['transcript_path'] = str(path)
        return path

    def stop(self):
        return self.hook(hook_event_name='Stop', stop_hook_active=False)

    def test_cli_only_reads_future_suffix_and_captures_assistant_text(self):
        path = self.native_file()
        historical = json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'content': 'Historical text must never transfer'}}) + '\n'
        path.write_text(historical)
        self.hook()
        with path.open('a') as stream:
            stream.write(json.dumps({'type': 'assistant', 'sessionId': self.payload['session_id'], 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Synthetic final reply'}, {'type': 'thinking', 'thinking': 'Never collect reasoning'}, {'type': 'tool_use', 'input': 'Never collect tools'}]}}) + '\n')
        _, result = self.stop()
        self.assertEqual(result['local_queue']['state'], 'confirmed')
        self.assertEqual(self.sent[-1]['params']['event']['messages'], [{'role': 'assistant', 'content': 'Synthetic final reply'}])
        self.assertNotIn('Historical text', json.dumps(self.sent))
        self.assertNotIn('Never collect', json.dumps(self.sent))
        self.stop()
        self.assertEqual(len(self.sent), 2)

    def test_ide_reads_only_exact_generation_message_files(self):
        path = self.native_file('.json')
        index = {'messages': [{'id': 'old-assistant', 'role': 'assistant'}, {'id': 'current-assistant', 'role': 'assistant'}],
                 'requests': [{'id': 'old-generation', 'messages': ['old-assistant']}, {'id': 'generation-1', 'messages': ['current-assistant']}]}
        path.write_text(json.dumps(index))
        messages = path.parent / 'messages'
        messages.mkdir()
        # The old referenced message is intentionally absent: no attempt may
        # be made to read it while collecting this generation.
        (messages / 'current-assistant.json').write_text(json.dumps({'id': 'current-assistant', 'role': 'assistant', 'message': json.dumps({'role': 'assistant', 'content': [{'type': 'text', 'text': 'IDE actual final answer'}]})}))
        self.hook()
        _, result = self.stop()
        self.assertEqual(result['local_queue']['state'], 'confirmed')
        self.assertEqual(self.sent[-1]['params']['event']['messages'][0]['content'], 'IDE actual final answer')

    def test_ide_persistence_lag_is_retried_on_a_later_hook(self):
        path = self.native_file('.json')
        path.write_text(json.dumps({'messages': [{'id': 'assistant', 'role': 'assistant', 'isComplete': False}],
                                    'requests': [{'id': 'generation-1', 'messages': ['assistant']}]}))
        self.hook()
        with self.assertRaises(ValueError):
            self.stop()
        path.write_text(json.dumps({'messages': [{'id': 'assistant', 'role': 'assistant', 'isComplete': True}],
                                    'requests': [{'id': 'generation-1', 'messages': ['assistant']}]}))
        messages = path.parent / 'messages'
        messages.mkdir()
        (messages / 'assistant.json').write_text(json.dumps({'id': 'assistant', 'role': 'assistant', 'message': {'content': 'Fully persisted IDE answer'}}))
        self.hook(hook_event_name='SessionStart')
        self.assertEqual(self.sent[-1]['params']['event']['messages'][0]['content'], 'Fully persisted IDE answer')

    def test_already_completed_ide_generation_is_not_a_future_capture_source(self):
        path = self.native_file('.json')
        path.write_text(json.dumps({'messages': [{'id': 'old-assistant', 'role': 'assistant'}],
                                    'requests': [{'id': 'generation-1', 'state': 'complete', 'messages': ['old-assistant']}]}))
        self.hook()
        self.stop()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]['params']['event']['messages'][0]['role'], 'user')

    def test_unknown_history_outside_path_and_rotation_never_transfers(self):
        path = self.native_file()
        path.write_text('')
        with self.assertRaises(ValueError):
            self.stop()
        self.hook()
        path.rename(path.with_suffix('.old'))
        path.write_text('{"type":"assistant","message":{"content":"wrong new file"}}\n')
        with self.assertRaises(ValueError):
            self.stop()
        self.assertEqual(len(self.sent), 1)
        self.payload['transcript_path'] = str(self.fixture.home / 'unrelated.jsonl')
        with self.assertRaises(ValueError):
            native_transcript.native_path(self.fixture.home, self.payload)

    def test_explicit_private_prompt_excludes_both_sides_of_the_turn(self):
        path = self.native_file()
        path.write_text('')
        self.payload['prompt'] = 'LocalOnly: synthetic private question'
        self.hook()
        path.write_text('{"type":"assistant","message":{"content":"private answer"}}\n')
        self.stop()
        self.assertEqual(self.sent, [])

    def test_sharing_question_keeps_both_sides_of_the_turn(self):
        path = self.native_file()
        path.write_text('')
        question = '为什么规则禁止跨项目共享？'
        self.payload['prompt'] = question
        output, result = self.hook()
        self.assertTrue(output)
        self.assertEqual(result['local_queue']['state'], 'confirmed')
        answer = '文档中的“仅限本项目”说明来源适用范围。'
        path.write_text(json.dumps({'type': 'assistant', 'message': {'content': answer}}) + '\n')
        _, result = self.stop()
        self.assertEqual(result['local_queue']['state'], 'confirmed')
        self.assertEqual([request['params']['event']['messages'] for request in self.sent],
                         [[{'role': 'user', 'content': question}], [{'role': 'assistant', 'content': answer}]])

    def test_explicit_sharing_request_keeps_both_sides_excluded_even_as_question(self):
        path = self.native_file()
        path.write_text('')
        self.payload['prompt'] = '请不要跨项目共享这条信息，可以吗？'
        output, result = self.hook()
        self.assertEqual(output, {})
        self.assertFalse(result['data_transferred'])
        path.write_text('{"type":"assistant","message":{"content":"synthetic private answer"}}\n')
        self.stop()
        self.assertEqual(self.sent, [])

    def test_body_shape_keeps_prompt_and_native_assistant_capture(self):
        path = self.native_file()
        path.write_text('')
        prompt = 'Synthetic configuration example password=example-only'
        self.payload['prompt'] = prompt
        output, result = self.hook()
        self.assertTrue(output)
        self.assertEqual(result['local_queue']['state'], 'confirmed')
        answer = 'Synthetic documentation: Bearer ' + 'synthetic' * 4
        path.write_text(json.dumps({'type': 'assistant', 'message': {'content': answer}}) + '\n')
        _, result = self.stop()
        self.assertEqual(result['local_queue']['state'], 'confirmed')
        self.assertEqual([request['params']['event']['messages'] for request in self.sent],
                         [[{'role': 'user', 'content': prompt}], [{'role': 'assistant', 'content': answer}]])

    def test_explicit_private_assistant_still_stops_before_queueing(self):
        self.hook()
        _, result = self.hook(hook_event_name='Stop', stop_hook_active=False,
                              last_assistant_message='[no-memory] synthetic private answer')
        self.assertEqual(result['status'], 'excluded-local-only-or-sensitive')
        self.assertEqual(len(self.sent), 1)


if __name__ == '__main__':
    unittest.main()
