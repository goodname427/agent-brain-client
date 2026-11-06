import agent_brain_client.interfaces.cli.collection as collection_cli
import agent_brain_client.adapters.storage.metadata as metadata
import agent_brain_client.domain.privacy as privacy
"""Isolated global routing/ownership fixtures; no native session or service."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import agent_brain_client.runtime.collection as gm
import agent_brain_client.adapters.harnesses.codex.collection_installation as install
from agent_brain_client.domain.scopes import select, source_id, namespace, digest


class GlobalMemoryFixtures(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.scope = {'schema': 1, 'profile_id': 'a' * 32, 'include_roots': [{'root': '/synthetic/personal', 'sharing': 'personal'}, {'root': '/synthetic/personal/isolated', 'sharing': 'project'}], 'exclude_roots': ['/synthetic/company'], 'non_project_roots': ['/synthetic/notes'], 'recall_personal': True}


    def origin(self, root):
        return {'kind': 'project', 'cwd': root + '/src', 'project_root': root, 'repository_root': root}

    def test_body_shape_is_not_a_privacy_verdict_but_explicit_markers_are(self):
        from agent_brain_client.domain.contract import private_recall_text
        from agent_brain_client.domain.scopes import restricted
        examples = ('password=example-only', 'api_key=synthetic-example',
                    'secret_key=synthetic-example', 'Bearer ' + 'synthetic' * 4,
                    'sk-' + 'synthetic' * 4,
                    '-----BEGIN ' + 'PRIVATE KEY-----\nsynthetic example')
        for text in examples:
            with self.subTest(text=text):
                self.assertFalse(privacy.private_content(text))
                self.assertFalse(private_recall_text(text))
                self.assertFalse(restricted(text))
                for marker in ('[LocalOnly]', '[no-memory]', '[private]', 'LocalOnly:', '不要记忆：'):
                    self.assertTrue(privacy.private_content(marker + ' ' + text))
                    self.assertTrue(private_recall_text(marker + ' ' + text))
                for marker in ('[no-share]', '[project-only]', '禁止跨项目共享', '仅限本项目'):
                    self.assertTrue(restricted(marker + ' ' + text))
                    self.assertTrue(private_recall_text(marker + ' ' + text))

    def test_sharing_keywords_in_questions_and_quotes_are_not_directives(self):
        from agent_brain_client.domain.contract import private_recall_text
        from agent_brain_client.domain.scopes import restricted
        questions = ('为什么规则禁止跨项目共享？', '禁止跨项目共享是什么意思？',
                     '仅限本项目是什么意思？', '如何理解“不要跨项目共享”这句话？',
                     '文档写着“仅限当前项目”，请解释。',
                     'Why does the policy say do not share this across projects?',
                     'What does keep this project-only mean?',
                     '"Do not share this" is a documentation example.')
        directives = ('禁止跨项目共享', '禁止跨项目共享这段内容',
                      '请不要跨项目共享这条信息，可以吗？', '仅限本项目使用。',
                      'Do not share this across projects, please.', 'Please keep this project-only.',
                      '[no-share] 为什么规则禁止跨项目共享？',
                      'Synthetic context\r\n\u3000禁止跨项目共享\r\n',
                      'Synthetic context\n[project-only] synthetic information')
        for text in questions:
            with self.subTest(question=text):
                self.assertFalse(restricted(text))
                self.assertFalse(private_recall_text(text))
        for text in directives:
            with self.subTest(directive=text):
                self.assertTrue(restricted(text))
                self.assertTrue(private_recall_text(text))

    def test_personal_sharing_project_isolation_and_worktree_deny(self):
        one, two = self.origin('/synthetic/personal/one'), self.origin('/synthetic/personal/two')
        self.assertEqual(namespace('owner', source_id(self.scope, one, select(self.scope, one)['sharing'])), namespace('owner', source_id(self.scope, two, select(self.scope, two)['sharing'])))
        private = self.origin('/synthetic/personal/isolated')
        self.assertNotEqual(source_id(self.scope, one, 'personal'), source_id(self.scope, private, select(self.scope, private)['sharing']))
        worktree = dict(one, repository_root='/SYNTHETIC/COMPANY/main')
        self.assertIsNone(select(self.scope, worktree))
        self.assertIsNone(select(self.scope, self.origin('/synthetic/personal-other')))
        self.assertIsNotNone(select(self.scope, {'kind': 'non-project', 'cwd': '/synthetic/notes', 'project_root': None, 'repository_root': None}))

    def test_unapproved_router_never_reads_stdin_or_activates_identity(self):
        value = {'enabled': True, 'scope': self.scope, 'approved_scope_sha256': None, 'server_scope_sha256': None}
        with patch.object(sys, 'stdin') as stdin, patch.object(gm, 'CollectionClient', side_effect=AssertionError('credential activation')):
            stdin.buffer.read.side_effect = AssertionError('unapproved body access')
            self.assertEqual(gm.hook(value), {})
        stdin.buffer.read.assert_not_called()

    def test_excluded_directory_is_rejected_before_repository_probe(self):
        value={'enabled':True,'device_home':str(self.root),'scope':self.scope,'approved_scope_sha256':digest(self.scope),'server_scope_sha256':digest(self.scope)}
        with patch.object(gm,'discover',side_effect=AssertionError('excluded repository probe')):
            self.assertIsNone(gm.eligible(value,'/synthetic/company/work'))

    def test_owned_project_capture_is_not_duplicated(self):
        local = self.origin('/synthetic/personal/one'); value = {'legacy_projects': [{'root': local['cwd']}]}
        payload = {'cwd': local['cwd'], 'hook_event_name': 'Stop'}
        with patch.object(gm, 'eligible', return_value=local), patch.object(sys, 'stdin', type('Input', (), {'buffer': io.BytesIO(json.dumps(payload).encode())})()), patch.object(gm, 'submit_hook', side_effect=AssertionError('duplicate capture')):
            self.assertEqual(gm.hook(value), {})

    def test_hook_failure_reports_stage_without_content_or_exception_text(self):
        from agent_brain_client.adapters.storage.content import operation_stage
        def failed(*args, **kwargs):
            with operation_stage('global-cache-open'):
                raise PermissionError('SYNTHETIC_SECRET path and request body')
        output, diagnostic = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, 'argv', ['global_memory', 'global-hook']),
            patch.object(gm, 'load', return_value={}),
            patch.object(gm, 'hook', side_effect=failed),
            patch.object(sys, 'stdout', output),
            patch.object(sys, 'stderr', diagnostic),
        ):
            self.assertEqual(collection_cli.main(), 0)
        self.assertEqual(output.getvalue(), '{}\n')
        result = json.loads(diagnostic.getvalue())
        self.assertEqual(result['operation_stage'], 'global-cache-open')
        self.assertEqual(result['error_type'], 'PermissionError')
        self.assertTrue(result['preserve_existing_state'])
        self.assertNotIn('SYNTHETIC_SECRET', diagnostic.getvalue())

    def test_cache_json_retains_hardlink_rejection(self):
        file = self.root / 'staged.json'
        file.write_text('{"synthetic":true}', encoding='utf-8')
        file.chmod(0o600)
        self.assertEqual(metadata.private_json(file, cache=True), {'synthetic':True})
        os.link(file, self.root / 'linked.json')
        with self.assertRaises(ValueError): metadata.private_json(file, cache=True)

    def test_question_drives_recall_without_promoting_other_project_rules(self):
        from agent_brain_client.domain.scopes import source_context, restricted
        local = self.origin('/synthetic/personal/one')
        foreign = self.origin('/synthetic/personal/two')
        context = source_context([foreign], local)
        self.assertEqual(context['relation'], 'other-source')
        self.assertFalse(context['global_rule'])
        item = {'content': 'Synthetic project convention', 'source_context': context}
        payload = {'cwd': local['cwd'], 'hook_event_name': 'UserPromptSubmit', 'session_id': 'session', 'turn_id': 'turn', 'prompt': 'How did the other project handle retries?'}
        value = {'scope': self.scope, 'legacy_projects': [], 'device_home': str(self.root)}
        def stdin(): return type('Input', (), {'buffer': io.BytesIO(json.dumps(payload).encode())})()
        with patch.object(gm, 'eligible', return_value=local), patch.object(gm, 'submit_hook', return_value={'status': 'staged'}), patch.object(gm, 'settings_path', return_value=self.root/'settings.json'), patch.object(gm, 'recall', return_value=[item]) as recall:
            with patch.object(sys, 'stdin', stdin()): result = gm.hook(value)
            self.assertEqual(recall.call_args.args[3], payload['prompt'])
            self.assertIn('do not apply to this project', result['hookSpecificOutput']['additionalContext'])
            with patch.object(sys, 'stdin', stdin()): self.assertEqual(gm.hook(value), {})
            self.assertEqual(recall.call_count, 1)
        self.assertTrue(restricted('禁止跨项目共享这段内容'))
        self.assertTrue(restricted('[project-only] synthetic information'))

    def test_migrated_legacy_source_is_collected_by_shared_router(self):
        local = self.origin('/synthetic/personal/one')
        value = {'legacy_projects': [{'root': local['cwd'], 'capture_delegated': False}]}
        self.assertIsNone(gm.legacy(value, local))

    def test_inline_install_reentry_uninstall_preserves_mcp_and_other_hooks(self):
        native, state = self.root / 'native', self.root / 'state'
        native.mkdir(mode=0o700); state.mkdir(mode=0o700)
        config = native / 'config.toml'
        config.write_text('[mcp_servers.fixture]\ncommand="fixture"\n[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype="command"\ncommand="fixture-only"\n')
        config.chmod(0o600); before = tomllib.loads(config.read_text())
        legacy = native / 'hooks.json'; legacy.write_text('{"fixture":"legacy-preserved"}')
        body = '# agent-brain-global-memory:begin\n[[hooks.SessionStart]]\n[[hooks.SessionStart.hooks]]\ntype="command"\ncommand="fixture"\nstatusMessage="Agent Brain global memory"\n# agent-brain-global-memory:end\n'
        install.transact(native, state, body)
        first = config.read_bytes(); install.transact(native, state, body)
        self.assertEqual(config.read_bytes(), first)
        after = tomllib.loads(config.read_text())
        self.assertEqual(after['mcp_servers'], before['mcp_servers'])
        self.assertEqual(after['hooks']['Stop'], before['hooks']['Stop'])
        self.assertEqual(legacy.read_text(), '{"fixture":"legacy-preserved"}')
        install.transact(native, state, '', remove=True)
        self.assertEqual(tomllib.loads(config.read_text()), before)

    def test_unknown_owned_block_edit_is_preserved(self):
        native, state = self.root / 'native', self.root / 'state'
        native.mkdir(mode=0o700); state.mkdir(mode=0o700)
        config = native / 'config.toml'; config.write_text('# agent-brain-global-memory:begin\n# foreign\n# agent-brain-global-memory:end\n'); config.chmod(0o600)
        before = config.read_bytes()
        with self.assertRaises(ValueError): install.transact(native, state, 'fixture')
        self.assertEqual(config.read_bytes(), before)


if __name__ == '__main__': unittest.main()
