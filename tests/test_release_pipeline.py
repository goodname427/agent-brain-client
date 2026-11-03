"""Synthetic release faults; never authenticate, push or contact a real service."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import release_client as rc


def fixture():
    assets = {}
    for kind, names in {'runtime': ['amd64.tar.gz', 'arm64.tar.gz', 'SHA256SUMS.txt'],
                        'installer': ['source.tar.gz', 'SHA256SUMS.txt']}.items():
        for name in names:
            assets[kind + '-' + name] = b'synthetic ' + name.encode()
    plan = {'source': '1' * 40, 'source_tree': '2' * 40, 'baseline': '3' * 40,
            'runtime': '4' * 40, 'version': '1.2.4', 'manifest_sha256': '5' * 64,
            'tags': {'runtime': 'client-1.2.4-windows', 'installer': 'installer-1.2.4-windows-setup'},
            'assets': {name: {'size': len(raw), 'sha256': rc.sha(raw)} for name, raw in assets.items()}}
    return plan, assets


class Remote:
    def __init__(self, plan, assets):
        self.plan, self.assets = plan, assets
        self.refs = {rc.CHANNEL: plan['baseline']}
        self.releases, self.events = {}, []
        self.latest = None
        self.lose_upload_reply = False
        self.race = False

    def ref(self, name):
        return self.refs.get(name)

    def git(self, *args, **kwargs):
        if args[0] == 'push':
            self.events.append(('push', args))
            if args[1].startswith('--force-with-lease='):
                if self.race:
                    self.refs[rc.CHANNEL] = '9' * 40
                    raise ValueError('Concurrent channel advance')
                self.assert_public()
                if self.refs[rc.CHANNEL] != self.plan['baseline']:
                    raise ValueError('Lease rejected')
            for spec in args:
                if ':' in spec and not spec.startswith('--'):
                    target, ref = spec.split(':', 1)
                    self.refs[ref] = target
        return ''

    def assert_public(self):
        assert len(self.releases) == 2
        assert all(not row['draft'] and len(row['assets']) in (2, 3) for row in self.releases.values())

    def ci(self, plan):
        return {'conclusion': 'success', 'source_tree': plan['source_tree']}

    def find_release(self, tag):
        return copy.deepcopy(next((row for row in self.releases.values() if row['tag_name'] == tag), None))

    def request(self, path, method='GET', value=None, raw=None, upload=False):
        if method != 'GET':
            self.events.append((method, path))
        if path == '/releases/latest':
            return self.find_release(self.latest)
        if path == '/releases' and method == 'POST':
            row = dict(value, id=len(self.releases) + 1, assets=[])
            self.releases[row['id']] = row
            return copy.deepcopy(row)
        rid = int(path.split('/')[2])
        row = self.releases[rid]
        if upload:
            name = path.split('name=')[1]
            row['assets'].append({'name': name, 'size': len(raw), 'digest': 'sha256:' + rc.sha(raw)})
            if self.lose_upload_reply:
                self.lose_upload_reply = False
                raise OSError('Reply lost after remote upload succeeded')
        elif method == 'PATCH':
            if value.get('make_latest') == 'true':
                assert self.refs[rc.CHANNEL] == self.plan['runtime']
                self.latest = row['tag_name']
            row.update(value)
        return copy.deepcopy(row)


class ReleasePipeline(unittest.TestCase):
    def test_archives_are_identical_and_preserve_exact_payload(self):
        payload = {'z/file.py': b'print("fixture")\n', 'a.txt': b'LF\n'}
        first = rc.archive(payload, 'fixture')
        self.assertEqual(first, rc.archive(dict(reversed(list(payload.items()))), 'fixture'))
        with tarfile.open(fileobj=io.BytesIO(first)) as archive:
            self.assertEqual({m.name.removeprefix('fixture/'): archive.extractfile(m).read()
                              for m in archive.getmembers()}, payload)
            self.assertTrue(all(m.uid == 0 and m.mtime == 0 for m in archive.getmembers()))

    def test_scope_credential_and_contract_changes_are_blocked(self):
        before = {'schema': 1, 'channel': 'stable', 'core_api': 1, 'state_schema': 1,
                  'python_min': '3.11', 'capabilities': ['fixture'], 'migrations': [],
                  'files': {'scripts/brain_windows.py': 'a' * 64, 'fixture.py': 'b' * 64}}
        changed = copy.deepcopy(before)
        changed['files']['fixture.py'] = 'c' * 64
        rc.compatible(before, changed)
        for mutate in (lambda m: m['files'].update({'new.py': 'd' * 64}),
                       lambda m: m['files'].update({'scripts/brain_windows.py': 'e' * 64}),
                       lambda m: m['capabilities'].append('expanded'),
                       lambda m: m['migrations'].append({'id': 'unreviewed'})):
            changed = copy.deepcopy(before)
            mutate(changed)
            with self.assertRaises(ValueError):
                rc.compatible(before, changed)

    def test_prepare_retry_keeps_frozen_version_and_assets(self):
        plan, assets = fixture()
        before = json.dumps({'version': '1.2.3'})
        def fake_git(*args, **kwargs):
            return before if args[0] == 'show' and ':' in args[-1] else 'fixture commit'
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            with patch.object(rc, 'commit', return_value=plan['baseline']), \
                    patch.object(rc, 'git', side_effect=fake_git), \
                    patch.object(rc, 'build', return_value=(plan, assets)) as build, \
                    patch.object(rc, 'source_payload'), \
                    patch.object(rc, 'validate'):
                rc.prepare(output, plan['source'], 'auto')
                frozen = (output / 'plan.json').read_bytes()
                rc.prepare(output, plan['source'], 'auto')
                self.assertEqual((output / 'plan.json').read_bytes(), frozen)
                self.assertEqual(build.call_args.args, (plan['source'], plan['baseline'], '1.2.4'))
                with self.assertRaises(ValueError):
                    rc.prepare(output, 'f' * 40, 'auto')
                name = next(iter(assets))
                (output / 'assets' / name).write_bytes(b'changed')
                with self.assertRaises(ValueError):
                    rc.prepare(output, plan['source'], 'auto')

    def test_source_checks_accept_official_checkout_url_and_reject_blob_drift(self):
        source = '1' * 40
        raw = b'synthetic source\n'
        def fake_git(*args):
            if args[0] == 'remote':
                return rc.ORIGIN.removesuffix('.git')
            if args[0] == 'status':
                return ''
            return source
        def fake_run(args):
            return b'100644 blob ' + b'2' * 40 + b'\tfixture.py\0' if args[1] == 'ls-tree' else raw
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'fixture.py'
            path.write_bytes(raw)
            with patch.object(rc, 'ROOT', root), patch.object(rc, 'git', side_effect=fake_git), \
                    patch.object(rc, 'run', side_effect=fake_run):
                self.assertEqual(rc.source_payload(source)[0], {'fixture.py': raw})
                path.write_bytes(b'local drift')
                with self.assertRaises(ValueError):
                    rc.source_payload(source)

    def test_public_download_corruption_is_rejected_without_auth_header(self):
        plan, assets = fixture()
        row = {'draft': False, 'prerelease': False, 'assets': [
            {'name': name, 'size': item['size'], 'digest': 'sha256:' + item['sha256']}
            for name, item in rc.assets_for(plan, 'runtime').items()]}
        def download(request, **kwargs):
            self.assertNotIn('Authorization', request.headers)
            return io.BytesIO(b'corrupt')
        with patch.object(rc, 'urlopen', side_effect=download), self.assertRaises(ValueError):
            rc.download_assets(plan, row, 'runtime')

    def publish_case(self, remote, output):
        with patch.object(rc, 'git', side_effect=remote.git), \
                patch.object(rc, 'remote_ref', side_effect=remote.ref), \
                patch.object(rc, 'download_assets', side_effect=lambda p, r, k: rc.check_assets(r, rc.assets_for(p, k), complete=True)):
            rc.publish(output, remote, remote.plan)

    def setup_output(self, output, assets):
        (output / 'assets').mkdir()
        for name, raw in assets.items():
            (output / 'assets' / name).write_bytes(raw)
        (output / 'release-notes.md').write_text('Synthetic release notes\n')

    def test_lost_upload_reply_resumes_then_repeated_publish_is_idempotent(self):
        plan, assets = fixture()
        remote = Remote(plan, assets)
        remote.lose_upload_reply = True
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            self.setup_output(output, assets)
            with self.assertRaises(OSError):
                self.publish_case(remote, output)
            self.assertEqual(remote.refs[rc.CHANNEL], plan['baseline'])
            self.publish_case(remote, output)
            uploaded = sum('/assets?' in path for event, path in remote.events if event == 'POST')
            self.publish_case(remote, output)
            self.assertEqual(sum('/assets?' in path for event, path in remote.events if event == 'POST'), uploaded)
            self.assertEqual(uploaded, len(assets))
            self.assertEqual(remote.refs[rc.CHANNEL], plan['runtime'])
            self.assertEqual(json.loads((output / 'progress.json').read_bytes())['production_catalog'], 'not-checked')

    def test_remote_conflict_or_missing_ci_fails_before_mutations(self):
        plan, assets = fixture()
        for failure in ('channel', 'tag', 'ci', 'asset'):
            remote = Remote(plan, assets)
            if failure == 'channel':
                remote.refs[rc.CHANNEL] = 'f' * 40
            elif failure == 'tag':
                remote.refs['refs/tags/' + plan['tags']['runtime']] = 'f' * 40
            elif failure == 'ci':
                remote.ci = lambda p: (_ for _ in ()).throw(ValueError('No matching CI'))
            else:
                remote.releases[1] = {'id': 1, 'tag_name': plan['tags']['runtime'], 'draft': True,
                                      'assets': [{'name': 'runtime-amd64.tar.gz', 'size': 1, 'digest': 'bad'}]}
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp)
                self.setup_output(output, assets)
                with self.assertRaises(ValueError):
                    self.publish_case(remote, output)
                self.assertEqual(remote.events, [], failure)

    def test_channel_race_keeps_other_publisher_and_does_not_move_latest(self):
        plan, assets = fixture()
        remote = Remote(plan, assets)
        remote.race = True
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            self.setup_output(output, assets)
            with self.assertRaises(ValueError):
                self.publish_case(remote, output)
            self.assertEqual(remote.refs[rc.CHANNEL], '9' * 40)
            self.assertIsNone(remote.latest)

    def test_ci_accepts_matching_pr_tree_and_rejects_failed_or_other_tree(self):
        plan, _ = fixture()
        api = rc.GitHub()
        rows = [{'head_sha': 'a' * 40, 'conclusion': 'cancelled', 'head_repository': {'full_name': rc.REPOSITORY}},
                {'head_sha': 'b' * 40, 'conclusion': 'success', 'head_repository': {'full_name': rc.REPOSITORY},
                 'event': 'pull_request', 'pull_requests': [{'base': {'ref': 'main', 'sha': 'c' * 40}}],
                 'id': 12, 'run_attempt': 1, 'html_url': 'https://example.invalid/run/12'}]
        def request(path):
            if '/compare/' in path:
                return {'status': 'ahead'}
            if '/jobs?' in path:
                return {'jobs': [{'name': name, 'conclusion': 'success'} for name in rc.FULL_CI_JOBS]}
            return {'workflow_runs': rows} if '/actions/' in path else {'tree': {'sha': plan['source_tree']}}
        with patch.object(api, 'request', side_effect=request):
            self.assertEqual(api.ci(plan)['run_id'], 12)
            rows[1]['conclusion'] = 'failure'
            with self.assertRaises(ValueError):
                api.ci(plan)

    def test_pr_merge_with_base_changes_cannot_certify_the_head_tree(self):
        plan, _ = fixture()
        api = rc.GitHub()
        row = {'head_sha': 'b' * 40, 'conclusion': 'success', 'head_repository': {'full_name': rc.REPOSITORY},
               'event': 'pull_request', 'pull_requests': [{'base': {'ref': 'main', 'sha': 'c' * 40}}],
               'id': 12, 'run_attempt': 1, 'html_url': 'https://example.invalid/run/12'}
        def request(path):
            if '/compare/' in path:
                return {'status': 'diverged'}
            if '/jobs?' in path:
                return {'jobs': [{'name': name, 'conclusion': 'success'} for name in rc.FULL_CI_JOBS]}
            return {'workflow_runs': [row]} if '/actions/' in path else {'tree': {'sha': plan['source_tree']}}
        with patch.object(api, 'request', side_effect=request):
            with self.assertRaises(ValueError):
                api.ci(plan)
            row.update(event='push', head_branch='main')
            self.assertEqual(api.ci(plan)['run_id'], 12)

    def test_scoped_success_cannot_certify_a_release_or_hide_an_older_full_run(self):
        plan, _ = fixture()
        api = rc.GitHub()
        row = {'head_sha': 'b' * 40, 'conclusion': 'success',
               'head_repository': {'full_name': rc.REPOSITORY},
               'event': 'workflow_dispatch', 'head_branch': 'main',
               'id': 12, 'run_attempt': 2, 'html_url': 'https://example.invalid/run/12'}
        jobs = [{'name': name, 'conclusion': 'success'} for name in rc.FULL_CI_JOBS]
        rows = [row]

        def request(path):
            if '/jobs?' in path:
                self.assertIn('/attempts/2/', path)
                return {'jobs': jobs}
            if '/actions/' in path:
                return {'workflow_runs': rows}
            return {'tree': {'sha': plan['source_tree']}}

        with patch.object(api, 'request', side_effect=request):
            self.assertEqual(api.ci(plan)['run_id'], 12)
            for job in jobs:
                for result in ('skipped', 'failure', 'cancelled', None):
                    job['conclusion'] = result
                    with self.subTest(job=job['name'], result=result):
                        with self.assertRaises(ValueError):
                            api.ci(plan)
                job['conclusion'] = 'success'
            removed = jobs.pop()
            with self.assertRaises(ValueError):
                api.ci(plan)
            jobs.append(removed)
            # A newer failed run for the same head must not mask actual full evidence.
            rows.insert(0, dict(row, id=13, conclusion='failure'))
            self.assertEqual(api.ci(plan)['run_id'], 12)
            row['head_branch'] = 'unmerged-branch'
            with self.assertRaises(ValueError):
                api.ci(plan)

    def test_ci_rejects_other_tree_and_incomplete_or_duplicate_job_evidence(self):
        plan, _ = fixture()
        api = rc.GitHub()
        row = {'id': 12, 'run_attempt': 1, 'head_sha': 'b' * 40, 'conclusion': 'success',
               'head_repository': {'full_name': rc.REPOSITORY}, 'event': 'push',
               'head_branch': 'main', 'html_url': 'https://example.invalid/run/12'}
        jobs = [{'name': name, 'conclusion': 'success'} for name in rc.FULL_CI_JOBS]
        tree = plan['source_tree']

        def request(path):
            if '/jobs?' in path:
                return {'jobs': jobs}
            if '/actions/' in path:
                return {'workflow_runs': [row]}
            return {'tree': {'sha': tree}}

        with patch.object(api, 'request', side_effect=request):
            tree = 'f' * 40
            with self.assertRaises(ValueError):
                api.ci(plan)
            tree = plan['source_tree']
            jobs.append(jobs[0])
            with self.assertRaises(ValueError):
                api.ci(plan)
        with patch.object(api, 'request', return_value={'jobs': [{'name': 'other', 'conclusion': 'success'}] * 100}):
            self.assertFalse(api.full_ci_jobs(row))


def platform_fixture(groups=('windows', 'macos')):
    assets, release_assets, tags, runtimes, channels = {}, {}, {}, {}, {}
    baselines = {rc.CHANNEL: '3' * 40, rc.MAC_CHANNELS[0]: '4' * 40, rc.MAC_CHANNELS[1]: '5' * 40}
    for index, group in enumerate(groups, 6):
        kind = 'runtime_' + group
        release_assets[kind] = [f'runtime-{group}-{arch}.tar.gz' for arch in ('one', 'two')]
        release_assets[kind].append(f'runtime-{group}-SHA256SUMS.txt')
        tags[kind] = 'client-1.2.4-' + group
        runtimes[group] = str(index) * 40
        for ref in rc.GROUPS[group]['channels']:
            channels[ref] = {'baseline': baselines[ref], 'runtime': runtimes[group]}
    release_assets['installer'] = ['installer-source.tar.gz', 'installer-SHA256SUMS.txt']
    tags['installer'] = 'installer-1.2.4-setup'
    for names in release_assets.values():
        assets.update({name: ('synthetic ' + name).encode() for name in names})
    plan = {'schema': 2, 'source': '1' * 40, 'source_tree': '2' * 40,
            'executor_source': 'a' * 40, 'executor_tree': 'b' * 40,
            'version': '1.2.4', 'groups': list(groups), 'baselines': baselines,
            'channels': channels, 'runtimes': runtimes, 'tags': tags,
            'manifests': {group: '8' * 64 for group in groups},
            'release_assets': release_assets,
            'assets': {name: {'sha256': rc.sha(raw), 'size': len(raw)} for name, raw in assets.items()}}
    return plan, assets


class PlatformRemote(Remote):
    def __init__(self, plan, assets):
        super().__init__({'baseline': '0' * 40}, assets)
        self.plan = plan
        self.refs = dict(plan['baselines'])
        self.ci_trees = []

    def ci(self, plan):
        self.ci_trees.append(plan['source_tree'])
        return {'conclusion': 'success', 'source_tree': plan['source_tree']}

    def assert_public(self):
        for kind in rc.targets(self.plan):
            row = self.find_release(self.plan['tags'][kind])
            assert row and not row['draft']
            rc.check_assets(row, rc.assets_for(self.plan, kind), complete=True)

    def git(self, *args, **kwargs):
        if args[0] != 'push':
            return ''
        self.events.append(('push', args))
        leases = [value for value in args if value.startswith('--force-with-lease=')]
        if leases:
            assert '--atomic' in args
            if self.race:
                self.refs[rc.MAC_CHANNELS[0]] = 'f' * 40
            for lease in leases:
                ref, baseline = lease.removeprefix('--force-with-lease=').split(':')
                if self.refs[ref] != baseline:
                    raise ValueError('Concurrent channel advance')
            self.assert_public()
        for value in args:
            if ':' in value and not value.startswith('--'):
                target, ref = value.split(':', 1)
                self.refs[ref] = target
        return ''

    def request(self, path, method='GET', value=None, raw=None, upload=False):
        if method == 'PATCH' and value.get('make_latest') == 'true':
            assert all(self.refs[ref] == row['runtime'] for ref, row in self.plan['channels'].items())
            self.events.append((method, path))
            row = self.releases[int(path.split('/')[2])]
            row.update(value)
            self.latest = row['tag_name']
            return copy.deepcopy(row)
        return super().request(path, method, value, raw, upload)


class PlatformRelease(unittest.TestCase):
    setup_output = ReleasePipeline.setup_output
    publish_case = ReleasePipeline.publish_case
    def test_mac_credential_source_pin_is_checked_independently(self):
        before = {'schema': 1, 'channel': 'stable', 'core_api': 1, 'state_schema': 1,
                  'python_min': '3.11', 'capabilities': ['fixture'], 'migrations': [],
                  'files': {'native/CredentialStore.swift': 'a' * 64, 'fixture.py': 'b' * 64}}
        after = copy.deepcopy(before)
        after['files']['fixture.py'] = 'c' * 64
        rc.compatible(before, after, 'native/CredentialStore.swift')
        after['files']['native/CredentialStore.swift'] = 'd' * 64
        with self.assertRaises(ValueError):
            rc.compatible(before, after, 'native/CredentialStore.swift')

    def test_real_runtime_commit_preserves_both_existing_mac_histories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = dict(os.environ, GIT_AUTHOR_NAME='Synthetic', GIT_COMMITTER_NAME='Synthetic',
                       GIT_AUTHOR_EMAIL='synthetic@example.invalid', GIT_COMMITTER_EMAIL='synthetic@example.invalid')
            def git(*args, data=None):
                return subprocess.check_output(['git', *args], cwd=root, env=env, input=data).decode().strip()
            git('init', '-q')
            tree = git('mktree', data=b'')
            first = git('commit-tree', tree, data=b'Synthetic first history\n')
            second = git('commit-tree', tree, data=b'Synthetic second history\n')
            source = git('commit-tree', tree, '-p', first, data=b'Synthetic source\n')
            with patch.object(rc, 'ROOT', root):
                runtime = rc.runtime_commit({'fixture.py': b'pass\n'}, (first, second), source, '1.2.4', 'macOS')
                self.assertEqual(runtime, rc.runtime_commit({'fixture.py': b'pass\n'}, (first, second), source, '1.2.4', 'macOS'))
            self.assertEqual(git('show', '-s', '--format=%P', runtime).split(), [first, second])
            for parent in (first, second):
                git('merge-base', '--is-ancestor', parent, runtime)
            self.assertEqual(git('show', runtime + ':fixture.py'), 'pass')

    def test_mac_catchup_builds_both_manifests_and_reuses_exact_published_installer(self):
        source = '1' * 40
        baselines = {ref: str(index) * 40 for index, ref in enumerate((rc.CHANNEL, *rc.MAC_CHANNELS), 3)}
        def git(*args, **kwargs):
            if '--format=%B' in args:
                return 'Source-Commit: ' + source
            if args[0] == 'rev-parse' and 'refs/tags/' in args[-1]:
                return source
            return '2' * 40
        def packages(payload, parents, source, selected, group, *, advance):
            return {f'runtime-{group}-{selected}.tar.gz': b'synthetic'}, ('7' * 40 if advance else None), '8' * 64
        with patch.object(rc, 'source_payload', return_value=({'fixture.py': b'pass\n'}, {'fixture.py': 0o644})), \
                patch.object(rc, 'executor_identity', return_value=('a' * 40, 'b' * 40)), \
                patch.object(rc, 'git', side_effect=git), patch.object(rc, 'run', return_value=(source + '\n').encode()), \
                patch.object(rc, 'git_tag_exists', return_value=True), \
                patch.object(rc, 'runtime_artifacts', side_effect=packages) as build:
            plan, assets = rc.build_platforms(source, baselines, '1.2.4', ['macos'])
        self.assertEqual([(call.args[4], call.kwargs['advance']) for call in build.call_args_list], [('windows', False), ('macos', True)])
        self.assertEqual(set(plan['validated_groups']), {'windows', 'macos'})
        self.assertEqual(plan['tags']['installer'], 'installer-1.2.4-windows-setup')
        self.assertEqual(set(plan['channels']), set(rc.MAC_CHANNELS))
        self.assertFalse(any('windows' in name for name in assets))

    def test_both_platforms_publish_before_atomic_channel_promotion(self):
        plan, assets = platform_fixture()
        remote = PlatformRemote(plan, assets)
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            self.setup_output(output, assets)
            self.publish_case(remote, output)
            self.assertEqual(remote.ci_trees, [plan['source_tree'], plan['executor_tree']])
            self.assertTrue(all(remote.refs[ref] == row['runtime'] for ref, row in plan['channels'].items()))
            self.assertEqual(remote.latest, plan['tags']['installer'])
            writes = [args for event, args in remote.events if event == 'push' and any('--force-with-lease=' in value for value in args)]
            self.assertEqual(len(writes), 1)
            self.assertEqual(sum(value.startswith('--force-with-lease=') for value in writes[0]), 3)

    def test_platform_prepare_retry_keeps_frozen_version_channels_and_assets(self):
        plan, assets = platform_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            with patch.object(rc, 'commit', return_value='3' * 40), \
                    patch.object(rc, 'git', return_value=json.dumps({'version': '1.2.3'})), \
                    patch.object(rc, 'build_platforms', return_value=(plan, assets)) as build, \
                    patch.object(rc, 'validate'), patch.object(rc, 'source_payload'):
                rc.prepare_platforms(output, plan['source'], 'auto', 'all')
                frozen = (output / 'plan.json').read_bytes()
                rc.prepare_platforms(output, plan['source'], 'auto', 'all')
                self.assertEqual((output / 'plan.json').read_bytes(), frozen)
                self.assertEqual(build.call_args.args[1:], (plan['baselines'], plan['version'], plan['groups'], plan['tags']['installer']))
                with self.assertRaises(ValueError):
                    rc.prepare_platforms(output, plan['source'], 'auto', 'macos')
                name = next(iter(assets))
                (output / 'assets' / name).write_bytes(b'changed')
                with self.assertRaises(ValueError):
                    rc.prepare_platforms(output, plan['source'], 'auto', 'all')

    def test_mac_catchup_retains_windows_and_does_not_upload_existing_installer(self):
        plan, assets = platform_fixture(('macos',))
        plan['tags']['installer'] = 'installer-1.2.4-windows-setup'
        remote = PlatformRemote(plan, assets)
        remote.releases[1] = {'id': 1, 'tag_name': plan['tags']['installer'], 'draft': False,
                              'assets': [{'name': name, 'size': row['size'], 'digest': 'sha256:' + row['sha256']}
                                         for name, row in rc.assets_for(plan, 'installer').items()]}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            self.setup_output(output, assets)
            self.publish_case(remote, output)
            self.assertEqual(remote.refs[rc.CHANNEL], plan['baselines'][rc.CHANNEL])
            uploads = [path for event, path in remote.events if event == 'POST' and '/assets?' in path]
            self.assertEqual(len(uploads), len(plan['release_assets']['runtime_macos']))
            self.assertFalse(any('/releases/1/assets?' in path for path in uploads))
            self.publish_case(remote, output)
            self.assertEqual(len([path for event, path in remote.events if event == 'POST' and '/assets?' in path]), len(uploads))

    def test_mac_alias_race_is_atomic_and_never_promotes_latest(self):
        plan, assets = platform_fixture(('macos',))
        remote = PlatformRemote(plan, assets)
        remote.race = True
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            self.setup_output(output, assets)
            with self.assertRaises(ValueError):
                self.publish_case(remote, output)
            self.assertEqual(remote.refs[rc.MAC_CHANNELS[1]], plan['baselines'][rc.MAC_CHANNELS[1]])
            self.assertIsNone(remote.latest)

    def test_executor_ci_or_unselected_platform_drift_stops_before_writes(self):
        for failure in ('executor-ci', 'windows-drift'):
            plan, assets = platform_fixture(('macos',))
            remote = PlatformRemote(plan, assets)
            if failure == 'executor-ci':
                remote.ci = lambda p: (_ for _ in ()).throw(ValueError('Executor CI missing')) if p['source_tree'] == plan['executor_tree'] else {}
            else:
                remote.refs[rc.CHANNEL] = 'f' * 40
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp)
                self.setup_output(output, assets)
                with self.assertRaises(ValueError):
                    self.publish_case(remote, output)
                self.assertEqual(remote.events, [])


if __name__ == '__main__':
    unittest.main()
