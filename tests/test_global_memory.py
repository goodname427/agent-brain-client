"""Isolated global routing/ownership fixtures; no native session or service."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import global_memory as gm
import global_memory_install as install
from global_memory_scope import select, source_id, namespace, digest


class GlobalMemoryFixtures(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.scope = {'schema': 1, 'profile_id': 'a' * 32, 'include_roots': [{'root': '/synthetic/personal', 'sharing': 'personal'}, {'root': '/synthetic/personal/isolated', 'sharing': 'project'}], 'exclude_roots': ['/synthetic/company'], 'non_project_roots': ['/synthetic/notes'], 'recall_personal': True}
        gm.old.imports()

    def origin(self, root):
        return {'kind': 'project', 'cwd': root + '/src', 'project_root': root, 'repository_root': root}

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
        with patch.object(sys, 'stdin') as stdin, patch.object(gm, 'GlobalClient', side_effect=AssertionError('credential activation')):
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

    def test_question_drives_recall_without_promoting_other_project_rules(self):
        from global_memory_scope import source_context, restricted
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
