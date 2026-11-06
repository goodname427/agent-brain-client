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
sys.path[:0] = [str(ROOT / p) for p in ('adapters/codebuddy', 'tools', 'tests')]
import host as cbhost
import runtime as cb
import transcript as native_transcript
import codebuddy as entry
import global_memory as gm
from global_memory_scope import digest
import test_content_outbox as fixture
from test_event_hook import accepted


class Configuration(unittest.TestCase):
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
        self.core = SimpleNamespace(old=gm.old)

    def install(self, **kwargs):
        return cbhost.install(self.home, ROOT, self.core, **kwargs)

    def test_install_repeat_uninstall_and_frozen_code_survive_source_checkout(self):
        if os.name == 'nt':
            from test_windows_native import host_security
            host_before=host_security(self.settings)
        result = self.install()
        self.assertTrue(result['installed'])
        from brain_lock import private_mode
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
        receipt = cb.read_json(cbhost.state_directory(self.home) / 'registration.json')
        command = receipt['mcp']
        entry_path = Path(command['args'][command['args'].index('-B') + 1])
        self.assertNotIn(str(ROOT), str(entry_path))
        self.assertEqual(entry_path.read_bytes(), (ROOT / 'tools/codebuddy.py').read_bytes())
        self.assertTrue(self.install(remove=True)['backend_preserved'])
        settings = cbhost.Document(self.settings.read_text()).value
        self.assertEqual(settings['permissions'], {'deny': ['Bash']})
        self.assertEqual(settings['hooks']['UserPromptSubmit'], [])
        self.assertNotIn('agent_brain_server', (self.native / '.mcp.json').read_text())
        self.assertFalse((self.home / '.codex').exists())
        self.install()

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

    def test_legacy_receipt_owner_repair_requires_the_original_private_journal(self):
        self.install()
        directory = cbhost.state_directory(self.home)
        file, journal = directory / 'registration.json', directory / 'transaction.json'
        record = gm.old.private_json(journal)
        record['host'][str(file)].pop('private')
        gm.old.write(journal, record)
        before = file.read_bytes()
        settings = self.settings.read_bytes()
        actual = gm.old.private_json
        failed = []
        def inherited_owner(path, *args, **kwargs):
            if Path(path) == file and not failed:
                failed.append(True)
                raise ValueError('Owned private Windows item required')
            return actual(path, *args, **kwargs)
        with patch.object(cbhost, 'os', SimpleNamespace(name='nt')), \
                patch.object(gm.old, 'private_json', side_effect=inherited_owner):
            self.assertEqual(cbhost.registration(file, journal, self.core), json.loads(before))
        self.assertEqual(file.read_bytes(), before)
        self.assertEqual(self.settings.read_bytes(), settings)
        from brain_lock import private_mode
        private_mode(file, owner=True)
        # A changed receipt cannot use the old owner failure to overwrite data.
        file.write_bytes(before + b' ')
        failed.clear()
        with patch.object(cbhost, 'os', SimpleNamespace(name='nt')), \
                patch.object(gm.old, 'private_json', side_effect=inherited_owner), \
                patch('brain_host_files.host_atomic') as write:
            with self.assertRaises(ValueError):
                cbhost.registration(file, journal, self.core)
            write.assert_not_called()
        self.assertEqual(file.read_bytes(), before + b' ')

    def test_failure_and_interrupted_transaction_restore_all_images(self):
        from brain_host_files import host_apply as real_apply
        def interrupted(rows, persist):
            real_apply(rows, persist)
            raise OSError('Synthetic post-write failure')
        with patch('brain_host_files.host_apply', side_effect=interrupted), self.assertRaises(OSError):
            self.install()
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertFalse((self.native / '.mcp.json').exists())
        self.assertFalse((cbhost.state_directory(self.home) / 'registration.json').exists())
        with patch('brain_host_files.host_apply', side_effect=lambda rows, persist: (real_apply(rows, persist), (_ for _ in ()).throw(SystemExit(7)))):
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
        self.core = SimpleNamespace(old=gm.old, approved=gm.approved, load=lambda home: self.value,
                                    adapter=lambda value, local: (self.client, self.queue),
                                    eligible=lambda value, cwd: self.local, recall=Mock(return_value=[{'content': 'Synthetic recalled memory'}]), hook_context=gm.hook_context)
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

    def test_stop_gate_sensitive_payload_and_capture_failure_keep_recall_independent(self):
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
            tools = root / 'tools'
            tools.mkdir()
            home = root / 'home'
            home.mkdir()
            log = root / 'operations'
            script = tools / 'install-codebuddy.sh'
            script.write_bytes((ROOT / 'tools/install-codebuddy.sh').read_bytes())
            (tools / 'codebuddy.py').write_text('import os,sys\nfrom pathlib import Path\nif sys.argv[1]=="backend-home": print(str(Path.home()/"shared-native"))\nelse: Path(os.environ["SYNTHETIC_LOG"]).open("a").write("bind\\n")\n')
            (tools / 'install-mac.sh').write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SYNTHETIC_LOG"\n')
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
        from brain_memory import ContentOutbox
        source = fixture.Outbox('test_same_id_cannot_change_request_or_cas_base')
        source.setUp()
        self.addCleanup(source.doCleanups)
        file = source.home / 'content-policy.json'
        gm.old.write(file, fixture.POLICY)
        source.cloud.queue_identity = {'transport': 'synthetic-shared-https'}
        core = SimpleNamespace(old=gm.old, GlobalClient=lambda value, local: source.cloud)
        registered = {'runtime': {'content_policy': str(file), 'content_policy_sha256': cb.digest(file.read_bytes()), 'codex_home': str(source.home / '.codex')}}
        local = {'kind': 'non-project', 'cwd': str(source.home), 'project_root': None, 'repository_root': None}
        with patch.object(gm.old, 'registration', return_value=({}, registered)):
            client, queue = cb.open_queue(core, {'device_home': str(source.home)}, local)
        self.assertIsInstance(queue, ContentOutbox)
        self.assertEqual(client.queue_identity['host_adapter'], 'codebuddy')
        self.assertIn('codebuddy/outboxes', queue.cache.as_posix())
        self.assertNotEqual(queue.queue_sha, source.adapter.queue_sha)
        self.assertFalse((source.home / '.codex').exists())

    def test_gateway_preserves_backend_argv_and_environment(self):
        command = {'command': '/synthetic/python', 'args': ['-I', '/synthetic/startup.py'], 'env': {'SYNTHETIC_CLIENT': '1'}}
        execute = Mock()
        with patch.object(cb, 'backend', return_value=(command, {})):
            cb.execute_mcp('/synthetic/home', object(), execute)
        self.assertEqual(execute.call_args.args[:2], ('/synthetic/python', ['/synthetic/python', '-I', '/synthetic/startup.py']))
        self.assertEqual(execute.call_args.args[2]['SYNTHETIC_CLIENT'], '1')

    def test_probe_requires_authenticated_protocol_result(self):
        command = {'command': '/synthetic/python', 'args': ['-I', '/synthetic/startup.py']}
        with patch.object(cb, 'backend', return_value=(command, {})):
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
            (state / 'codex-mcp.json').write_text(json.dumps({'release_policy': str(policy), 'release_policy_sha256': cb.digest(policy.read_bytes())}))
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
        metadata = cb.read_json(fixture.home / '.config/agent-brain-client/codex-mcp.json')
        policy = cb.read_json(metadata['release_policy'])
        self.assertEqual(policy['runtime']['codex_home'], str(fixture.native))
        self.assertFalse((fixture.home / '.codex').exists())
        bound = cbhost.install(fixture.home, ROOT, SimpleNamespace(old=gm.old))
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

    def test_sensitive_prompt_excludes_both_sides_of_the_turn(self):
        path = self.native_file()
        path.write_text('')
        self.payload['prompt'] = 'LocalOnly: synthetic private question'
        self.hook()
        path.write_text('{"type":"assistant","message":{"content":"private answer"}}\n')
        self.stop()
        self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
