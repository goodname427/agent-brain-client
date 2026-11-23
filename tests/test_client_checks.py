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


def receipt(root, scope, python='3.12.15', tree=TREE, system=None):
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
           'python': python, 'system': system or ('win32' if scope == 'windows' else 'darwin'),
           'machine': 'synthetic', 'result': 'passed', 'checks': checks}
    if scope == 'release':
        row['seconds'] = 35.12
    path = directory / 'checks.json'
    path.write_bytes(cc.encoded(row))
    return path


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

    def test_only_recorded_macos_full_suite_has_a_longer_timeout(self):
        from agent_brain_client.adapters.platforms import filesystem

        for scope, record, expected in (
            ('macos', True, [3600, 1800, 1800, 1800, 1800, 1800]),
            ('macos', False, [1800] * len(cc.COMMANDS['macos'])),
            ('windows', True, [1800] * len(cc.COMMANDS['windows'])),
        ):
            with self.subTest(scope=scope, record=record), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(cc, 'check_host'), patch.object(cc, 'identity', return_value={'source_tree': TREE}), \
                    patch.object(filesystem, 'native_platform', return_value='synthetic'), \
                    patch.object(cc.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
                cc.run_checks(scope, Path(tmp) / 'checks', record=record)
                self.assertEqual([call.kwargs['timeout'] for call in run.call_args_list], expected)

    def test_macos_full_suite_timeout_keeps_log_without_a_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'checks'
            with patch.object(cc, 'check_host'), patch.object(cc, 'identity', return_value={'source_tree': TREE}), \
                    patch.object(cc.subprocess, 'run', side_effect=subprocess.TimeoutExpired('unittest', 3600)) as run:
                with self.assertRaises(subprocess.TimeoutExpired):
                    cc.run_checks('macos', output, record=True)
            self.assertTrue((output / '0.log').exists())
            self.assertFalse((output / 'checks.json').exists())
            run.assert_called_once()
            self.assertEqual(run.call_args.kwargs['timeout'], 3600)

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

    def test_publication_consumes_one_same_tree_result_from_either_platform(self):
        for system in ('darwin', 'win32'):
            plan, assets = fixtures.platform_fixture()
            remote = fixtures.PlatformRemote(plan, assets)
            with self.subTest(system=system), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = receipt(root, 'release', tree=plan['source_tree'], system=system)
                fixtures.ReleasePipeline.setup_output(self, root, assets)
                with patch.object(rc, 'git', side_effect=remote.git), \
                        patch.object(rc, 'remote_ref', side_effect=remote.ref), \
                        patch.object(rc, 'download_assets'), patch.object(cc, 'run_checks') as run:
                    rc.publish(root, remote, plan, preflight_result=path)
                run.assert_not_called()
                self.assertEqual(remote.latest, plan['tags']['installer'])
                self.assertEqual(remote.ci_sources, [])
                self.assertEqual(json.loads((root / 'preflight/000001/result.json').read_bytes())['system'], system)

    def test_default_preflight_runs_the_product_tree_and_rejects_wrong_tree(self):
        plan, _ = fixtures.platform_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            expected = {'result': 'passed'}
            product = output / 'product'
            with patch.object(rc, 'ROOT', product), patch.object(rc, 'git') as git, \
                    patch.object(cc, 'run_checks') as run, \
                    patch.object(cc, 'load_receipt', return_value=(('release', (3, 12)), expected)) as load:
                git.return_value = plan['executor_tree']
                with self.assertRaisesRegex(ValueError, 'candidate source tree'):
                    rc.publication_preflight(output, plan)
                run.assert_not_called()
                git.return_value = plan['source_tree']
                result = rc.publication_preflight(output, plan)
                checks = output / 'preflight/000002/checks'
                run.assert_called_once_with('release', checks, record=True, root=product)
                load.assert_called_once_with(checks / 'checks.json', plan['source_tree'])
                self.assertEqual(result, expected)

    def test_explicit_skip_reports_not_run_and_preserves_failed_attempts(self):
        plan, assets = fixtures.platform_fixture()
        remote = fixtures.PlatformRemote(plan, assets)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixtures.ReleasePipeline.setup_output(self, root, assets)
            with patch.object(rc, 'git', side_effect=remote.git), \
                    patch.object(rc, 'remote_ref', side_effect=remote.ref), \
                    patch.object(rc, 'download_assets'), patch.object(cc, 'run_checks') as run:
                rc.publish(root, remote, plan, skip_preflight_tests=True)
                run.assert_not_called()
                self.assertEqual(remote.ci_sources, [])
                journal = root / 'preflight/000001/result.json'
                self.assertEqual(json.loads(journal.read_bytes())['result'], 'not-run')
                (root / 'preflight/000002').mkdir()
                with self.assertRaisesRegex(ValueError, 'failed or was interrupted'):
                    rc.publish(root, remote, plan, skip_preflight_tests=True)
                self.assertFalse((root / 'preflight/000002/result.json').exists())

    def test_release_result_rejects_over_budget_and_wrong_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = receipt(Path(tmp), 'release')
            with self.assertRaisesRegex(ValueError, 'does not match'):
                cc.load_receipt(path, '9' * 40)
            row = json.loads(path.read_bytes())
            row['seconds'] = 300.001
            path.write_bytes(cc.encoded(row))
            with self.assertRaisesRegex(ValueError, '5-minute budget'):
                cc.load_receipt(path, TREE)

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
        for name in ('bootstrap/windows/install.ps1', 'bin/memory.ps1', 'bootstrap/windows/python.ps1'):
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
        self.assertEqual([call.kwargs['timeout'] for call in run.call_args_list], [1800] * len(commands))
        self.assertIn('dev/checks/tests.py', commands[0])
        self.assertFalse(any('dev/build/credentials.py' in command for command in commands))
        self.assertIn(['sh', '-n', 'bootstrap/macos/install.sh'], commands)


if __name__ == '__main__':
    unittest.main()
