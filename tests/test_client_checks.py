"""Local release evidence must never turn absent native checks into success."""
import copy
import json
import re
import shlex
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import client_checks as cc
import release_client as rc
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

    def test_local_commands_cover_every_workflow_check_and_keep_native_matrix(self):
        workflow = (cc.ROOT / '.github/workflows/client-checks.yml').read_text()
        parts = re.split(r'^  ([a-z-]+):\n', workflow.split('\njobs:\n', 1)[1], flags=re.M)
        jobs = dict(zip(parts[1::2], parts[2::2]))
        mapping = {'documentation': 'documentation', 'source': 'source-checks',
                   'macos': 'isolated-client', 'windows': 'windows-contracts'}
        for scope, job in mapping.items():
            for line in jobs[job].splitlines():
                words = shlex.split(line.strip())
                if 'python' in words:
                    words = words[words.index('python'):]
                    if words[:4] == ['python', '-m', 'pip', 'install']:
                        continue
                elif words[:2] in (['ruff', 'check'], ['mypy', '--check-untyped-defs']):
                    words = ['python', '-m', *words]
                elif words[:2] not in (['git', 'diff'], ['sh', '-n']):
                    continue
                words = [word.replace('$TEST_ROOT/', '{temp}/') for word in words]
                if words[:4] == ['python', '-B', '-m', 'unittest']:
                    covered = {item for command in cc.COMMANDS[scope]
                               if command[:4] == words[:4] for item in command[4:]}
                    self.assertTrue(set(words[4:]) <= covered, (scope, words))
                else:
                    self.assertIn(words, cc.COMMANDS[scope], scope)
        self.assertIn("python: ['3.11', '3.13']", workflow)
        self.assertIn('ruff==0.14.0 mypy==1.18.2', workflow)
        parsed = re.findall(r"ParseFile\(\(Join-Path \$pwd '([^']+)'", jobs['windows-contracts'])
        self.assertEqual(set(parsed), {'tools/install-windows.ps1', 'tools/memory-windows.ps1'})
        for name in parsed:
            self.assertIn(name, cc.COMMANDS['windows'][-1][-1])


if __name__ == '__main__':
    unittest.main()
