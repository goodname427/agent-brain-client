"""Real Git histories and fault injection for scoped CI; no network or device state."""
import ast
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ci_scope as ci


class Classification(unittest.TestCase):
    def assert_scope(self, paths, mac, windows):
        plan = ci.classify(paths)
        self.assertEqual((plan['code'], plan['mac'], plan['windows'], plan['full']),
                         (mac or windows, mac, windows, mac and windows))

    def test_docs_platform_shared_and_mixed(self):
        cases = [(['README.md', 'docs/installation/windows.md'], False, False),
                 (['native/CredentialStore.swift'], True, False),
                 (['tools/install-mac.sh', 'docs/README.md'], True, False),
                 (['tools/install-windows.ps1'], False, True),
                 (['installer/access_windows.py', 'tests/test_windows_native.py'], False, True),
                 (['installer/first_install.py'], True, True),
                 (['installer/windows_install_complete.py'], True, True),
                 (['scripts/brain_lock.py'], True, True),
                 (['scripts/brain_content.py'], True, True),
                 (['tests/fixtures/windows_platform_050/brain_lock.py.fixture'], True, True),
                 (['tools/install-mac.sh', 'tools/install-windows.ps1'], True, True),
                 (['docs/README.md', 'connect.py'], True, True)]
        for paths, mac, windows in cases:
            with self.subTest(paths=paths):
                self.assert_scope(paths, mac, windows)

    def test_unknown_dependencies_workflows_and_fixed_data_are_full(self):
        for path in ('.github/workflows/client-checks.yml', '.github/workflows/client-release.yml',
                     'tools/build_client.py',
                     'tools/release_client.py', 'requirements.txt', 'pyproject.toml',
                     'releases/manifest.json', 'FREEZE-0.5.json', '.gitattributes',
                     'docs/connection.example.json', 'docs/new.py', 'new/file',
                     'tests/README.md', '../README.md', '/README.md'):
            with self.subTest(path=path):
                self.assert_scope([path], True, True)
        self.assert_scope([], True, True)
        self.assert_scope(['docs/%s.md' % index for index in range(1001)] + ['connect.py'], True, True)

    def test_manual_merge_queue_missing_and_unrecognized_events_run_full(self):
        for name, event in (('workflow_dispatch', {}), ('merge_group', {}),
                            ('push', {'before': '0' * 40, 'after': 'a' * 40}),
                            ('push', {'before': 'not-a-sha', 'after': 'a' * 40}),
                            ('push', {}), ('pull_request', {}), ('unknown', {})):
            with self.subTest(name=name, event=event):
                self.assertTrue(ci.plan_event(name, event)['full'])
        with patch.object(ci, 'git', side_effect=OSError('Synthetic unavailable history')):
            self.assertTrue(ci.plan_event('push', {'before': 'a' * 40, 'after': 'b' * 40})['full'])


class FocusedTests(unittest.TestCase):
    def test_parallel_worker_isolates_homes_and_preserves_failure(self):
        import run_ci_tests
        result = subprocess.CompletedProcess([], 7, 'synthetic failure')
        with patch.object(run_ci_tests.subprocess, 'run', return_value=result) as run:
            code, log, _ = run_ci_tests.run_group(['test_synthetic'])
        self.assertEqual((code, log), (7, 'synthetic failure'))
        env = run.call_args.kwargs['env']
        self.assertNotEqual(env['HOME'], os.environ['HOME'])
        self.assertEqual(env['HOME'], env['USERPROFILE'])
        self.assertNotEqual(env['HOME'], env['CODEX_HOME'])
        self.assertEqual(run.call_args.args[0][-1], 'test_synthetic')

    def test_fixture_consumers_are_transitive_and_broken_test_parse_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'tests').mkdir()
            for name, source in (('test_a.py', 'pass'), ('test_b.py', 'import test_a'),
                                 ('test_c.py', 'from tests import test_b')):
                (root / 'tests' / name).write_text(source)
            self.assertEqual(ci.test_selection(['tests/test_a.py'], root),
                             ['test_a', 'test_b', 'test_c'])
            (root / 'tests/test_c.py').write_text('invalid syntax !')
            self.assertIsNone(ci.test_selection(['tests/test_a.py'], root))

    def test_test_only_modules_include_fixture_consumers_and_exclude_native_helper(self):
        plan = ci.classify(['tests/test_mac_first_install.py'])
        self.assertFalse(plan['full'])
        self.assertTrue(plan['mac'])
        self.assertIn('test_client_uninstall', plan['tests'])
        self.assertIn('test_mac_first_install', plan['tests'])
        self.assertTrue(ci.classify(['tests/fixtures/checkpoint_probe.py'])['full'])
        self.assertTrue(ci.classify(['tests/test_missing.py'])['full'])
        self.assertTrue(ci.classify(['tests/test_mac_first_install.py', 'scripts/brain_lock.py'])['full'])

    def test_windows_contract_test_edits_keep_both_native_versions(self):
        plan = ci.classify(['tests/test_windows_native.py'])
        self.assertTrue(plan['windows'])
        self.assertFalse(plan['full'])

    def test_ci_only_does_not_start_native_jobs(self):
        plan = ci.classify(['tools/ci_scope.py', 'tests/test_ci_scope.py', 'README.md'])
        self.assertTrue(plan['code'])
        self.assertFalse(plan['mac'] or plan['windows'] or plan['full'])

    def test_test_runner_rejects_shell_fragments_and_failed_tests(self):
        env = dict(os.environ, CI_TESTS='["test_dummy; echo injected"]')
        result = subprocess.run([sys.executable, '-B', str(ci.ROOT / 'tools/run_ci_tests.py')],
                                env=env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)


class GitHistory(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic CI')
        self.git('config', 'user.email', 'ci@example.invalid')
        self.write('README.md', 'Synthetic documentation.\n')
        self.write('docs/page.md', 'Synthetic documentation page.\n')
        self.write('tools/install-windows.ps1', '# Synthetic Windows\n')
        self.write('native/CredentialStore.swift', '// Synthetic Mac\n')
        self.base = self.commit()

    def git(self, *args):
        return ci.git(*args, root=self.root).decode().strip()

    def write(self, name, raw):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw)

    def commit(self):
        self.git('add', '--all')
        self.git('commit', '-qm', 'Synthetic scope fixture')
        return self.git('rev-parse', 'HEAD')

    def push(self, base=None):
        return ci.plan_event('push', {'before': base or self.base,
                                     'after': self.git('rev-parse', 'HEAD')}, self.root)

    def test_doc_punctuation_multi_commit_push_and_delete(self):
        self.write('README.md', 'Synthetic documentation!\n')
        self.commit()
        self.write('docs/space and\nnewline.md', 'Synthetic link fixture\n')
        self.commit()
        self.assertFalse(self.push()['code'])
        (self.root / 'tools/install-windows.ps1').unlink()
        self.commit()
        self.assertEqual((self.push()['mac'], self.push()['windows']), (False, True))

    def test_renames_include_both_names_even_code_to_markdown(self):
        for old, new, expected in (
            ('docs/page.md', 'docs/renamed.md', (False, False)),
            ('tools/install-windows.ps1', 'docs/windows.md', (False, True)),
            ('native/CredentialStore.swift', 'tools/install-windows.ps1', (True, True)),
        ):
            with self.subTest(old=old, new=new):
                self.git('reset', '--hard', self.base)
                (self.root / new).parent.mkdir(parents=True, exist_ok=True)
                os.replace(self.root / old, self.root / new)
                self.commit()
                paths = ci.changed_paths(self.base, self.git('rev-parse', 'HEAD'), self.root)
                self.assertIn(old, paths)
                self.assertIn(new, paths)
                plan = self.push()
                self.assertEqual((plan['mac'], plan['windows']), expected)

    def test_root_markdown_file_set_changes_need_full_package_validation(self):
        for action in ('delete', 'rename', 'add'):
            with self.subTest(action=action):
                self.git('reset', '--hard', self.base)
                if action == 'delete':
                    (self.root / 'README.md').unlink()
                elif action == 'rename':
                    os.replace(self.root / 'README.md', self.root / 'docs/renamed.md')
                else:
                    self.write('NEW.md', 'Synthetic root documentation.\n')
                self.commit()
                self.assertTrue(self.push()['full'])
        self.git('reset', '--hard', self.base)
        (self.root / 'docs/page.md').unlink()
        self.commit()
        self.assertFalse(self.push()['code'])

    def test_pr_uses_merge_base_not_only_last_commit_or_updated_base(self):
        self.git('checkout', '-qb', 'synthetic-pr')
        self.write('tools/install-windows.ps1', '# Changed Windows\n')
        self.commit()
        self.write('README.md', 'Changed punctuation!\n')
        head = self.commit()
        self.git('checkout', '--detach', self.base)
        self.write('native/CredentialStore.swift', '// Updated base\n')
        base = self.commit()
        plan = ci.plan_event('pull_request', {'pull_request': {'base': {'sha': base},
                                                              'head': {'sha': head}}}, self.root)
        self.assertEqual((plan['mac'], plan['windows']), (False, True))
        # Force-push across divergent histories takes the safe full route.
        self.assertTrue(ci.plan_event('push', {'before': head, 'after': base}, self.root)['full'])

    def test_empty_missing_history_symlinks_and_mode_changes_run_full(self):
        self.assertTrue(self.push()['full'])
        self.assertTrue(self.push('a' * 40)['full'])
        (self.root / 'README.md').chmod(0o755)
        self.commit()
        self.assertTrue(self.push()['full'])
        self.git('reset', '--hard', self.base)
        (self.root / 'README.md').unlink()
        (self.root / 'README.md').symlink_to('tools/install-windows.ps1')
        self.commit()
        self.assertTrue(self.push()['full'])


class Results(unittest.TestCase):
    @staticmethod
    def needs(mac=False, windows=False):
        flags = ci.selection(mac, windows)
        return {'changes': {'result': 'success', 'outputs': {
                    key: str(flags[key]).lower() for key in ('code', 'mac', 'windows', 'full')}},
                'documentation': {'result': 'success'},
                'source-checks': {'result': 'success' if mac or windows else 'skipped'},
                'isolated-client': {'result': 'success' if mac else 'skipped'},
                'windows-contracts': {'result': 'success' if windows else 'skipped'}}

    def test_selected_jobs_must_succeed_skips_never_certify_a_platform(self):
        for mac, windows in ((False, False), (True, False), (False, True), (True, True)):
            needs = self.needs(mac, windows)
            ci.verify_results(needs)
            for job in needs:
                for state in ('failure', 'cancelled', 'skipped'):
                    broken = copy.deepcopy(needs)
                    broken[job]['result'] = state
                    if state != needs[job]['result']:
                        with self.subTest(mac=mac, windows=windows, job=job, state=state):
                            with self.assertRaises(ValueError):
                                ci.verify_results(broken)

    def test_missing_and_inconsistent_outputs_fail(self):
        for key, value in (('mac', ''), ('code', 'true'), ('full', 'true')):
            needs = self.needs()
            needs['changes']['outputs'][key] = value
            with self.assertRaises(ValueError):
                ci.verify_results(needs)
        with self.assertRaises(KeyError):
            ci.verify_results({})

    def test_cli_writes_scope_and_verifies_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            event, output, summary = [Path(tmp) / name for name in ('event', 'output', 'summary')]
            event.write_text('{}')
            env = dict(os.environ, GITHUB_EVENT_NAME='workflow_dispatch',
                       GITHUB_EVENT_PATH=str(event), GITHUB_OUTPUT=str(output),
                       GITHUB_STEP_SUMMARY=str(summary), CI_NEEDS=json.dumps(self.needs()))
            for args in ([], ['verify']):
                subprocess.run([sys.executable, '-B', str(ci.ROOT / 'tools/ci_scope.py'), *args],
                               env=env, capture_output=True, check=True)
            self.assertIn('full=true', output.read_text())
            self.assertIn('false means not validated', summary.read_text())


class WorkflowContract(unittest.TestCase):
    def test_workflow_selection_required_gate_and_release_jobs_stay_aligned(self):
        source = (ci.ROOT / '.github/workflows/client-checks.yml').read_text()
        triggers, source_jobs = source.split('\njobs:\n', 1)
        self.assertNotRegex(triggers, r'\bpaths(?:-ignore)?:')
        self.assertIn('  workflow_dispatch:', triggers)
        self.assertIn('  merge_group:', triggers)
        parts = re.split(r'^  ([a-z-]+):\n', source_jobs, flags=re.M)
        jobs = dict(zip(parts[1::2], parts[2::2]))
        for flag, job in (('code', 'source-checks'), ('mac', 'isolated-client'),
                          ('windows', 'windows-contracts')):
            self.assertIn('    needs: changes\n', jobs[job])
            self.assertIn("    if: needs.changes.outputs.%s == 'true'\n" % flag, jobs[job])
        required = jobs['required']
        self.assertIn('    if: always()\n', required)
        self.assertIn('needs: [changes, documentation, source-checks, isolated-client, windows-contracts]', required)
        self.assertIn('run: python -B tools/ci_scope.py verify', required)
        self.assertIn('    needs: [changes, required]\n', jobs['full-validation'])
        self.assertIn("    if: needs.changes.outputs.full == 'true'\n", jobs['full-validation'])
        self.assertNotIn('\n    if:', jobs['documentation'])
        self.assertIn('fetch-depth: 0', jobs['changes'])
        # Every checkout must test the immutable event tree, including PR merges.
        self.assertEqual(source.count('uses: actions/checkout@'), source.count('ref: ${{ github.sha }}'))
        names = set()
        for job, body in jobs.items():
            label = re.search(r'^    name: (.+)$', body, re.M)
            name = label[1] if label else job
            versions = re.search(r'^        python: (.+)$', body, re.M)
            if versions:
                names.update(name + ' (' + version + ')' for version in ast.literal_eval(versions[1]))
            else:
                names.add(name)
        release = ast.parse((ci.ROOT / 'tools/release_client.py').read_text())
        contract = next(node.value for node in release.body if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == 'FULL_CI_JOBS'
                                for target in node.targets))
        self.assertEqual(names, ast.literal_eval(contract.args[0]))


if __name__ == '__main__':
    unittest.main()
