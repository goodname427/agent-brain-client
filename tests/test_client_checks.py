"""Local release evidence must never turn absent native checks into success."""
import copy
import json
import re
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import dev.checks.client as cc
import dev.release.client as rc
import test_release_pipeline as fixtures


TREE = '2' * 40


def receipt(root, scope, python='3.12.15', tree=TREE):
    directory = root / (scope + '-' + python + '-' + tree[:5])
    directory.mkdir()
    checks = []
    for index, command in enumerate(cc.COMMANDS[scope]):
        raw = f'synthetic check {index}\n'.encode()
        name = f'{index}.log'
        (directory / name).write_bytes(raw)
        checks.append({'command': command, 'exit_code': 0, 'log': name, 'sha256': cc.sha(raw)})
    row = {'schema': 1, 'mode': 'local', 'github_actions': 'not-used',
           'source': '1' * 40, 'source_tree': tree, 'scope': scope,
           'python': python, 'system': 'win32' if scope == 'windows' else 'darwin',
           'machine': 'synthetic', 'result': 'passed', 'checks': checks}
    path = directory / 'checks.json'
    path.write_bytes(cc.encoded(row))
    return path


def complete(root, tree=TREE):
    return [receipt(root, scope, python, tree) for scope, python in
            (('documentation', '3.12.15'), ('source', '3.12.15'), ('macos', '3.12.15'),
             ('windows', '3.11.15'), ('windows', '3.13.10'))]


class LocalChecks(unittest.TestCase):
    def test_native_scope_and_exact_python_tool_versions_are_enforced(self):
        for scope, system, python in (('macos', 'linux', (3, 12)),
                                      ('windows', 'darwin', (3, 11)),
                                      ('windows', 'win32', (3, 12)),
                                      ('source', 'darwin', (3, 13))):
            with self.subTest(scope=scope, system=system, python=python):
                with patch.object(cc.sys, 'platform', system), patch.object(cc.sys, 'version_info', python):
                    with self.assertRaises(ValueError):
                        cc.check_host(scope)
        with patch.object(cc.sys, 'version_info', (3, 12)), patch.object(cc, 'version', return_value='wrong'):
            with self.assertRaises(ValueError):
                cc.check_host('source')

    def test_failed_check_keeps_log_but_never_emits_receipt_or_runs_next_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'checks'
            with patch.object(cc, 'check_host'), patch.object(cc, 'identity', return_value={}), \
                    patch.object(cc.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)) as run:
                with self.assertRaises(ValueError):
                    cc.run_checks('documentation', output, record=True)
            self.assertTrue((output / '0.log').exists())
            self.assertFalse((output / 'checks.json').exists())
            self.assertEqual(run.call_count, 1)

    def test_run_isolates_homes_strips_token_and_checks_source_afterward(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'checks'
            with patch.object(cc, 'check_host'), \
                    patch.object(cc, 'identity', side_effect=[{'source': 'a'}, {'source': 'b'}]), \
                    patch.dict(cc.os.environ, {'GH_TOKEN': 'synthetic', 'GITHUB_TOKEN': 'synthetic'}), \
                    patch.object(cc.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
                with self.assertRaisesRegex(ValueError, 'Source changed'):
                    cc.run_checks('documentation', output, record=True)
            env = run.call_args.kwargs['env']
            self.assertNotIn('GH_TOKEN', env)
            self.assertNotIn('GITHUB_TOKEN', env)
            self.assertNotEqual(env['HOME'], cc.os.environ['HOME'])
            self.assertEqual(env['HOME'], env['USERPROFILE'])
            self.assertFalse((output / 'checks.json').exists())
            with patch.object(cc, 'check_host'):
                with self.assertRaises(FileExistsError):
                    cc.run_checks('documentation', output)

    def test_complete_evidence_checks_both_source_and_executor_trees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = complete(root)
            plan = {'source_tree': TREE, 'executor_tree': '3' * 40}
            with self.assertRaisesRegex(ValueError, 'executor'):
                cc.release_evidence(paths, plan)
            paths += complete(root, '3' * 40)
            result = cc.release_evidence(paths, plan)
            self.assertEqual(result['github_actions'], 'not-used')
            self.assertEqual(len(result['trees']['source']), 5)
            for index in range(len(paths)):
                with self.assertRaises(ValueError):
                    cc.release_evidence(paths[:index] + paths[index + 1:], plan)
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                cc.release_evidence(paths + [paths[0]], plan)

    def test_tampered_logs_missing_commands_wrong_host_and_failed_results_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = receipt(Path(tmp), 'windows', '3.11.15')
            before = json.loads(path.read_bytes())
            for change in (lambda row: row.update(system='darwin'),
                           lambda row: row.update(python='3.12.15'),
                           lambda row: row.update(github_actions='success'),
                           lambda row: row.update(source_tree='9' * 40),
                           lambda row: row['checks'].pop(),
                           lambda row: row['checks'][0].update(exit_code=1),
                           lambda row: row['checks'][0].update(log='../0.log')):
                row = copy.deepcopy(before)
                change(row)
                path.write_bytes(cc.encoded(row))
                with self.assertRaises(ValueError):
                    cc.load_receipt(path, TREE)
            path.write_bytes(cc.encoded(before))
            (path.parent / '0.log').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'log changed'):
                cc.load_receipt(path, TREE)

    def test_local_publication_reuses_release_checks_without_calling_actions(self):
        plan, assets = fixtures.platform_fixture()
        plan['executor_tree'] = plan['source_tree']
        remote = fixtures.PlatformRemote(plan, assets)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = complete(root, plan['source_tree'])
            with patch.object(rc, 'git', side_effect=remote.git), \
                    patch.object(rc, 'remote_ref', side_effect=remote.ref), \
                    patch.object(rc, 'download_assets'), \
                    patch.object(remote, 'ci', side_effect=AssertionError('Actions must not be queried')):
                for missing in range(5):
                    with self.assertRaises(ValueError):
                        rc.publish(root, remote, plan, local_checks=paths[:missing] + paths[missing + 1:])
                    self.assertEqual(remote.events, [])
                fixtures.ReleasePipeline.setup_output(self, root, assets)
                rc.publish(root, remote, plan, local_checks=paths)
            self.assertEqual(remote.latest, plan['tags']['installer'])
            self.assertEqual(json.loads((root / 'ci.json').read_bytes())['mode'], 'local')

    def test_prepare_local_checks_fail_before_build_and_windows_cannot_prepare(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(rc.sys, 'platform', 'darwin'), patch.object(rc.sys, 'version_info', (3, 12)), \
                patch.object(rc, 'source_payload'), patch.object(rc, 'git'), \
                patch.object(cc, 'run_checks', side_effect=ValueError('synthetic failure')), \
                patch.object(rc, 'prepare_platforms') as build:
            with self.assertRaisesRegex(ValueError, 'synthetic failure'):
                rc.prepare_local(Path(tmp), '1' * 40, 'auto', 'all')
            build.assert_not_called()
        with patch.object(rc.sys, 'platform', 'win32'):
            with self.assertRaisesRegex(ValueError, 'macOS'):
                rc.prepare_local(Path('unused'), '1' * 40, 'auto', 'all')

    def test_hybrid_requires_all_local_mac_scopes_and_keeps_official_windows_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = complete(root)[:3]
            plan = {'source_tree': TREE, 'executor_tree': '3' * 40}
            api = unittest.mock.Mock()
            api.windows_ci.return_value = {'scope': 'windows-jobs-only', 'run_id': 12}
            with self.assertRaisesRegex(ValueError, 'executor'):
                cc.hybrid_evidence(paths, plan, api)
            paths += complete(root, '3' * 40)[:3]
            result = cc.hybrid_evidence(paths, plan, api)
            self.assertEqual(result['mode'], 'hybrid')
            for label in ('source', 'executor'):
                self.assertEqual(len(result['trees'][label]['local']), 3)
                self.assertEqual(result['trees'][label]['github_windows']['scope'], 'windows-jobs-only')
            for index in range(len(paths)):
                with self.assertRaises(ValueError):
                    cc.hybrid_evidence(paths[:index] + paths[index + 1:], plan, api)
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                cc.hybrid_evidence(paths + [paths[0]], plan, api)
            with self.assertRaisesRegex(ValueError, 'Unexpected'):
                cc.hybrid_evidence(paths + [receipt(root, 'source', tree='9' * 40)], plan, api)
            with self.assertRaises(ValueError):
                cc.hybrid_evidence(paths + [root / ('windows-3.11.15-' + TREE[:5]) / 'checks.json'], plan, api)

    def test_hybrid_rejects_missing_windows_before_any_remote_write_and_reuses_publication(self):
        plan, assets = fixtures.platform_fixture()
        plan['executor_tree'] = plan['source_tree']
        remote = fixtures.PlatformRemote(plan, assets)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = complete(root, plan['source_tree'])[:3]
            with patch.object(rc, 'git', side_effect=remote.git), \
                    patch.object(rc, 'remote_ref', side_effect=remote.ref), \
                    patch.object(rc, 'download_assets'), \
                    patch.object(remote, 'windows_ci', create=True, side_effect=ValueError('Windows missing')) as windows:
                with self.assertRaisesRegex(ValueError, 'Windows missing'):
                    rc.publish(root, remote, plan, local_checks=paths, hybrid_checks=True)
                self.assertEqual(remote.events, [])
                windows.side_effect = None
                windows.return_value = {'scope': 'windows-jobs-only', 'run_id': 12}
                fixtures.ReleasePipeline.setup_output(self, root, assets)
                windows.reset_mock()
                rc.publish(root, remote, plan, local_checks=paths, hybrid_checks=True)
                windows.assert_called_once_with({'source_tree': plan['source_tree']})
            self.assertEqual(remote.latest, plan['tags']['installer'])
            self.assertEqual(json.loads((root / 'ci.json').read_bytes())['mode'], 'hybrid')

    def test_ci_invokes_the_same_inventory_and_keeps_native_matrix(self):
        workflow = (cc.ROOT / '.github/workflows/client-checks.yml').read_text()
        parts = re.split(r'^  ([a-z-]+):\n', workflow.split('\njobs:\n', 1)[1], flags=re.M)
        jobs = dict(zip(parts[1::2], parts[2::2]))
        mapping = {'documentation': 'documentation', 'source': 'source-checks',
                   'macos': 'isolated-client', 'windows': 'windows-contracts'}
        for scope, job in mapping.items():
            self.assertIn('dev/checks/client.py ' + scope + ' --ci --output', jobs[job])
            self.assertNotIn('python -B -m unittest', jobs[job])
            self.assertNotIn('ruff check', jobs[job])
        self.assertIn("python: ['3.11', '3.13']", workflow)
        self.assertIn('ruff==0.14.0 mypy==1.18.2', workflow)
        self.assertIn('test_codebuddy', {item for command in cc.COMMANDS['windows'] for item in command})
        self.assertIn('test_native_source_files', {item for command in cc.COMMANDS['windows'] for item in command})
        for name in ('bootstrap/windows/install.ps1', 'bin/memory.ps1', 'bootstrap/windows/codebuddy.ps1'):
            self.assertIn(name, cc.COMMANDS['windows'][-1][-1])
        self.assertTrue(any('src' in command and 'ruff' in command for command in cc.COMMANDS['source']))

    def test_windows_parallel_ci_keeps_every_selector_and_non_test_command(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(cc, 'check_host'), \
                patch.object(cc.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            cc.run_checks('windows', Path(tmp) / 'checks', ci=True)
        actual = [call.args[0] for call in run.call_args_list]
        original = cc.COMMANDS['windows']
        expected_tests = [test for command in original
                          if command[:4] == ['python', '-B', '-m', 'unittest'] for test in command[4:]]
        selectors = actual[0][actual[0].index('--modules') + 1:]
        self.assertEqual(selectors, list(dict.fromkeys(expected_tests)))
        self.assertEqual(actual[0][3:6], ['--workers', '2', '--modules'])
        expected_other = [command for command in original
                          if command[:4] != ['python', '-B', '-m', 'unittest']]
        self.assertEqual(actual[1:], [[cc.sys.executable if i == 0 and item == 'python' else item
                                     for i, item in enumerate(command)] for command in expected_other])

    def test_focused_ci_cannot_be_recorded_as_complete_local_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'complete local'):
                cc.run_checks('macos', Path(tmp) / 'checks', record=True, ci=True)

    def test_focused_ci_uses_selected_tests_and_skips_only_full_run_helpers(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(cc, 'check_host'), \
                patch.dict(cc.os.environ, {'CI_TESTS': '["test_codebuddy"]'}), \
                patch.object(cc.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            cc.run_checks('macos', Path(tmp) / 'checks', ci=True)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn('dev/checks/tests.py', commands[0])
        self.assertFalse(any('dev/build/credentials.py' in command for command in commands))
        self.assertTrue(any('bootstrap/macos/codebuddy.sh' in command for command in commands))


if __name__ == '__main__':
    unittest.main()
