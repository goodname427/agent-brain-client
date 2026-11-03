"""Synthetic release faults; never authenticate, push or contact a real service."""
import copy
import io
import json
from pathlib import Path
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
                 'id': 12, 'html_url': 'https://example.invalid/run/12'}]
        def request(path):
            if '/compare/' in path:
                return {'status': 'ahead'}
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
               'id': 12, 'html_url': 'https://example.invalid/run/12'}
        def request(path):
            if '/compare/' in path:
                return {'status': 'diverged'}
            return {'workflow_runs': [row]} if '/actions/' in path else {'tree': {'sha': plan['source_tree']}}
        with patch.object(api, 'request', side_effect=request):
            with self.assertRaises(ValueError):
                api.ci(plan)
            row.update(event='push', head_branch='main')
            self.assertEqual(api.ci(plan)['run_id'], 12)


if __name__ == '__main__':
    unittest.main()
