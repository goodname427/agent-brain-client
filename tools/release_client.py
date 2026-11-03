"""Repeatable Windows release preparation, explicit publication and readback.

Preparation needs only this clean Client checkout. Publication uses the existing
Git authentication and GH_TOKEN, never provisions credentials or changes policy.
"""
import argparse
from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from build_client import release
from publication_check import require_publishable

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'goodname427/agent-brain-client'
ORIGIN = 'https://github.com/' + REPOSITORY + '.git'
CHANNEL = 'refs/heads/releases/personal-https-windows-stable'
TRACKING = 'refs/remotes/origin/releases/personal-https-windows-stable'
PLATFORMS = ('win32-amd64', 'win32-arm64')
API = 'https://api.github.com/repos/' + REPOSITORY


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def run(args, *, data=None, env=None, cwd=ROOT):
    result = subprocess.run(args, input=data, capture_output=True, cwd=cwd,
                            env=env, timeout=600)
    if result.returncode:
        # Subprocess stderr can contain credential-helper output or private paths.
        raise ValueError('Command failed: ' + Path(args[0]).name + ' ' + args[1])
    return result.stdout


def git(*args, data=None, env=None):
    return run(['git', *args], data=data, env=env).decode().strip()


def commit(ref):
    return git('rev-parse', '--verify', ref + '^{commit}')


def version(value):
    if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('Canonical major.minor.patch version required')
    return tuple(map(int, value.split('.')))


def immutable(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError('Existing output differs: ' + path.name)
        return
    pending = path.with_suffix(path.suffix + '.pending')
    pending.write_bytes(raw)
    os.replace(pending, path)


def report(output, phase, **values):
    value = dict(phase=phase, **values)
    pending = output / 'progress.pending'
    pending.write_bytes(encoded(value))
    os.replace(pending, output / 'progress.json')
    print(json.dumps(value), flush=True)


@contextmanager
def locked(output):
    output.mkdir(parents=True, exist_ok=True)
    lock = output / 'release.lock'
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError('Release output is locked; check its owner before removing release.lock') from None
    try:
        os.write(descriptor, str(os.getpid()).encode())
        os.close(descriptor)
        yield
    finally:
        lock.unlink()


def source_payload(source):
    if commit('HEAD') != source or git('status', '--porcelain'):
        raise ValueError('A clean checkout of the exact source commit is required')
    if git('remote', 'get-url', 'origin') not in (ORIGIN, ORIGIN.removesuffix('.git')):
        raise ValueError('Official credential-free Client origin required')
    payload, modes = {}, {}
    rows = run(['git', 'ls-tree', '-rz', source]).split(b'\0')
    for row in filter(None, rows):
        metadata, name = row.split(b'\t', 1)
        mode, kind, oid = metadata.decode().split()
        name = name.decode()
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise ValueError('Linked or external source prohibited: ' + name)
        raw = run(['git', 'cat-file', 'blob', oid])
        path = ROOT / name
        if path.is_symlink() or path.read_bytes() != raw:
            raise ValueError('Checkout bytes differ from source blob: ' + name)
        payload[name], modes[name] = raw, int(mode[-3:], 8)
    require_publishable(payload)
    return payload, modes


def archive(payload, prefix, modes=None):
    result = io.BytesIO()
    with gzip.GzipFile(fileobj=result, mode='wb', filename='', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as out:
            for name, raw in sorted(payload.items()):
                item = tarfile.TarInfo(prefix + '/' + name)
                item.size = len(raw)
                item.mode = modes[name] if modes else 0o644
                out.addfile(item, io.BytesIO(raw))
    return result.getvalue()


def compatible(before, after):
    for key in ('schema', 'channel', 'core_api', 'state_schema', 'python_min',
                'capabilities', 'migrations'):
        if before[key] != after[key]:
            raise ValueError('Release contract change requires review: ' + key)
    if set(before['files']) != set(after['files']):
        raise ValueError('Release file scope change requires review')
    if before['files']['scripts/brain_windows.py'] != after['files']['scripts/brain_windows.py']:
        raise ValueError('Credential component change requires policy review')


def runtime_commit(payload, baseline, source, selected):
    with tempfile.TemporaryDirectory(prefix='client-release-index-') as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / 'index'))
        git('read-tree', '--empty', env=env)
        for name, raw in sorted(payload.items()):
            oid = git('hash-object', '-w', '--stdin', data=raw)
            git('update-index', '--add', '--cacheinfo', '100644', oid, name, env=env)
        tree = git('write-tree', env=env)
    timestamp = git('show', '-s', '--format=%ct', source) + ' +0000'
    env = dict(os.environ, GIT_AUTHOR_NAME='Agent Brain Release',
               GIT_AUTHOR_EMAIL='release@example.invalid',
               GIT_COMMITTER_NAME='Agent Brain Release',
               GIT_COMMITTER_EMAIL='release@example.invalid',
               GIT_AUTHOR_DATE=timestamp, GIT_COMMITTER_DATE=timestamp)
    message = f'Windows runtime {selected}\n\nSource-Commit: {source}\n'
    return git('commit-tree', tree, '-p', baseline, data=message.encode(), env=env)


def build(source, baseline, selected):
    payload, modes = source_payload(source)
    before = json.loads(git('show', baseline + ':releases/manifest.json'))
    baseline_source = commit('refs/tags/installer-' + before['version'] + '-windows-setup')
    git('merge-base', '--is-ancestor', baseline_source, source)
    if version(selected) <= version(before['version']):
        raise ValueError('New version must be greater than the frozen channel baseline')
    artifacts, manifests, packages = {}, [], []
    with tempfile.TemporaryDirectory(prefix='client-release-build-') as tmp:
        for platform in PLATFORMS:
            target = Path(tmp) / platform
            manifest = release(ROOT, target, selected, 'personal-content-https', platform)
            compatible(before, manifest)
            package = {p.relative_to(target).as_posix(): p.read_bytes()
                       for p in target.rglob('*') if p.is_file()}
            for name, digest in manifest['files'].items():
                if package[name] != payload[name] or sha(package[name]) != digest:
                    raise ValueError('Runtime source integrity failed: ' + name)
            run([sys.executable, '-I', '-B', str(target / 'adapters/codex/content_mcp.py'), '--self-check'])
            archive_payload = {n: raw for n, raw in package.items() if n != 'export-manifest.json'}
            arch = platform.split('-', 1)[1]
            name = f'agent-brain-client-runtime-windows-{arch}-{selected}'
            artifacts[name + '.tar.gz'] = archive(archive_payload, name)
            manifests.append(manifest)
            packages.append(package)
    if manifests[0] != manifests[1] or packages[0] != packages[1]:
        raise ValueError('Windows architectures require distinct reviewed channel payloads')
    name = f'agent-brain-client-installer-{selected}-source'
    artifacts[name + '.tar.gz'] = archive(payload, name, modes)
    for kind in ('installer', 'runtime'):
        sums = ''.join(sha(raw) + '  ' + name + '\n' for name, raw in sorted(artifacts.items())
                       if name.endswith('.tar.gz') and kind in name)
        artifacts[f'{kind}-{selected}-SHA256SUMS.txt'] = sums.encode()
    runtime = runtime_commit(packages[0], baseline, source, selected)
    manifest_sha = sha(json.dumps(manifests[0], sort_keys=True).encode())
    plan = {'schema': 1, 'repository': REPOSITORY, 'source': source,
            'source_tree': git('rev-parse', source + '^{tree}'), 'baseline': baseline,
            'baseline_source': baseline_source,
            'version': selected, 'runtime': runtime, 'channel': CHANNEL,
            'manifest_sha256': manifest_sha, 'platforms': list(PLATFORMS),
            'tags': {'runtime': f'client-{selected}-windows',
                     'installer': f'installer-{selected}-windows-setup'},
            'assets': {name: {'sha256': sha(raw), 'size': len(raw)}
                       for name, raw in sorted(artifacts.items())}}
    return plan, artifacts


def validate(output):
    with tempfile.TemporaryDirectory(prefix='client-release-tests-') as tmp:
        home, codex = Path(tmp) / 'home', Path(tmp) / 'codex'
        home.mkdir()
        codex.mkdir()
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(codex),
                   PYTHONPATH=os.pathsep.join(str(ROOT / p) for p in ('scripts', 'adapters/codex', 'tools', 'tests')))
        commands = ([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests'],
                    [sys.executable, '-B', 'tools/check_source.py'],
                    ['git', 'diff', '--check'])
        for index, command in enumerate(commands):
            with (output / f'check-{index}.log').open('wb') as log:
                result = subprocess.run(command, env=env, cwd=ROOT, stdout=log,
                                        stderr=subprocess.STDOUT, timeout=600)
            if result.returncode:
                raise ValueError(f'Local check {index} failed; inspect check-{index}.log')


def prepare(output, source, selected):
    existing = output / 'plan.json'
    if existing.exists():
        old = json.loads(existing.read_bytes())
        if old['source'] != source or selected not in ('auto', old['version']):
            raise ValueError('Output belongs to another source/version; use a separate directory')
        baseline, selected = old['baseline'], old['version']
    else:
        baseline = commit(TRACKING)
        before = json.loads(git('show', baseline + ':releases/manifest.json'))
        if selected == 'auto':
            major, minor, patch = version(before['version'])
            selected = f'{major}.{minor}.{patch + 1}'
    report(output, 'building', source=source, version=selected)
    plan, artifacts = build(source, baseline, selected)
    immutable(existing, encoded(plan))
    for name, raw in artifacts.items():
        immutable(output / 'assets' / name, raw)
    notes = (f"Agent Brain Client {selected} — Windows\n\n"
             f"Source: {source}\nRuntime: {plan['runtime']}\n"
             f"Manifest SHA256: {plan['manifest_sha256']}\n\n"
             + git('show', '-s', '--format=%s', source)
             + '\n\nExisting Windows channel ancestry, file scope, capabilities and credential component retained.\n'
             + 'Mac channel unchanged by this release. Desktop/ARM64 hardware and model reload acceptance are separate.\n')
    immutable(output / 'release-notes.md', notes.encode())
    report(output, 'validating', source=source, version=selected)
    validate(output)
    source_payload(source)
    report(output, 'prepared', source=source, version=selected, runtime=plan['runtime'],
           published=False, production_catalog='not-checked')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Authenticated API redirect rejected')


class GitHub:
    def request(self, path, method='GET', value=None, raw=None, upload=False):
        token = os.environ.get('GH_TOKEN')
        if not token and method != 'GET':
            raise ValueError('GH_TOKEN from existing authorized authentication is required')
        base = 'https://uploads.github.com/repos/' + REPOSITORY if upload else API
        headers = {'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'agent-brain-release'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        data = encoded(value) if value is not None else raw
        if data is not None:
            headers['Content-Type'] = 'application/json' if value is not None else 'application/octet-stream'
        try:
            with build_opener(NoRedirect()).open(Request(base + path, data=data, method=method, headers=headers), timeout=120) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 404 and method == 'GET':
                return None
            raise ValueError(f'GitHub {method} failed: HTTP {error.code}; read remote state before retry') from None

    def find_release(self, tag):
        for page in range(1, 101):
            rows = self.request(f'/releases?per_page=100&page={page}')
            for row in rows:
                if row['tag_name'] == tag:
                    return row
            if len(rows) < 100:
                return None
        raise ValueError('Release pagination limit reached')

    def ci(self, plan):
        seen = set()
        for page in range(1, 11):
            result = self.request(f'/actions/workflows/client-checks.yml/runs?per_page=100&page={page}')
            for row in result['workflow_runs']:
                head = row['head_sha']
                if head in seen:
                    continue
                seen.add(head)
                if row['conclusion'] != 'success' or row['head_repository']['full_name'] != REPOSITORY:
                    continue
                candidate = self.request('/git/commits/' + head)
                if candidate['tree']['sha'] == plan['source_tree']:
                    return {'run_id': row['id'], 'url': row['html_url'], 'head': head,
                            'source_tree': plan['source_tree'], 'conclusion': 'success'}
            if len(result['workflow_runs']) < 100:
                break
        raise ValueError('Successful Client checks for the exact source tree required (PR or main)')


def assets_for(plan, kind):
    return {name: row for name, row in plan['assets'].items() if kind in name}


def check_assets(row, expected, *, complete):
    actual = {item['name']: item for item in row['assets']}
    if len(actual) != len(row['assets']) or set(actual) - set(expected):
        raise ValueError('Unexpected release assets retained; review required')
    if complete and set(actual) != set(expected):
        raise ValueError('Release assets incomplete')
    for name, item in actual.items():
        if item.get('digest') != 'sha256:' + expected[name]['sha256'] or item['size'] != expected[name]['size']:
            raise ValueError('Existing remote asset differs; never overwrite: ' + name)


def remote_ref(ref):
    rows = git('ls-remote', '--refs', 'origin', ref).splitlines()
    if not rows:
        return None
    if len(rows) != 1 or rows[0].split()[1] != ref:
        raise ValueError('Ambiguous remote ref')
    return rows[0].split()[0]


def load_bundle(output):
    saved = json.loads((output / 'plan.json').read_bytes())
    plan, artifacts = build(saved['source'], saved['baseline'], saved['version'])
    if saved != plan:
        raise ValueError('Prepared plan differs from rebuilt source')
    for name, raw in artifacts.items():
        if (output / 'assets' / name).read_bytes() != raw:
            raise ValueError('Prepared asset changed: ' + name)
    return plan


def targets(plan):
    return {'runtime': plan['runtime'], 'installer': plan['source']}


def preflight_remote(plan):
    if remote_ref(CHANNEL) not in (plan['baseline'], plan['runtime']):
        raise ValueError('Channel advanced elsewhere; keep this bundle, prepare a new version')
    for kind, target in targets(plan).items():
        actual = remote_ref('refs/tags/' + plan['tags'][kind])
        if actual not in (None, target):
            raise ValueError('Existing tag differs; never move: ' + plan['tags'][kind])


def download_assets(plan, row, kind):
    check_assets(row, assets_for(plan, kind), complete=True)
    if row['draft'] or row['prerelease']:
        raise ValueError('Public stable release required')
    for name, expected in assets_for(plan, kind).items():
        url = f"https://github.com/{REPOSITORY}/releases/download/{plan['tags'][kind]}/{name}"
        # Public downloads carry no Authorization header, including redirects.
        with urlopen(Request(url, headers={'User-Agent': 'agent-brain-release-check'}), timeout=120) as response:
            raw = response.read(expected['size'] + 1)
        if len(raw) != expected['size'] or sha(raw) != expected['sha256']:
            raise ValueError('Public download differs: ' + name)


def verify(output, api, plan):
    for kind, target in targets(plan).items():
        if remote_ref('refs/tags/' + plan['tags'][kind]) != target:
            raise ValueError('Published tag target unconfirmed')
        row = api.find_release(plan['tags'][kind])
        if row is None:
            raise ValueError('Published release not found')
        download_assets(plan, row, kind)
    if remote_ref(CHANNEL) != plan['runtime']:
        raise ValueError('Published channel target unconfirmed')
    latest = api.request('/releases/latest')
    if latest is None or latest['tag_name'] != plan['tags']['installer']:
        raise ValueError('Latest installer pointer unconfirmed')
    report(output, 'published-and-verified', source=plan['source'], version=plan['version'],
           runtime=plan['runtime'], public_assets_verified=len(plan['assets']),
           releases={kind: f'https://github.com/{REPOSITORY}/releases/tag/{tag}'
                     for kind, tag in plan['tags'].items()},
           production_catalog='not-checked', desktop_acceptance='not-checked')


def publish(output, api, plan):
    git('fetch', '--no-tags', 'origin', 'main')
    git('merge-base', '--is-ancestor', plan['source'], 'FETCH_HEAD')
    preflight_remote(plan)
    ci = api.ci(plan)
    (output / 'ci.json').write_bytes(encoded(ci))
    notes = (output / 'release-notes.md').read_bytes()
    require_publishable({'release-notes.md': notes})
    # Validate all existing releases before making any remote change.
    releases = {}
    for kind in targets(plan):
        row = api.find_release(plan['tags'][kind])
        if row:
            check_assets(row, assets_for(plan, kind), complete=not row['draft'])
        releases[kind] = row
    report(output, 'publishing-tags', version=plan['version'])
    git('push', '--atomic', 'origin', *(target + ':refs/tags/' + plan['tags'][kind]
                                      for kind, target in targets(plan).items()))
    for kind, target in targets(plan).items():
        report(output, 'publishing-' + kind, version=plan['version'])
        row = releases[kind]
        if row is None:
            row = api.request('/releases', 'POST', {
                'tag_name': plan['tags'][kind], 'target_commitish': target,
                'name': f"Agent Brain Client {plan['version']} {kind} (Windows)",
                'body': notes.decode(), 'draft': True, 'prerelease': False, 'make_latest': 'false'})
        if row['draft']:
            names = {item['name'] for item in row['assets']}
            for name in assets_for(plan, kind):
                if name not in names:
                    api.request(f"/releases/{row['id']}/assets?name=" + quote(name, safe=''),
                                'POST', raw=(output / 'assets' / name).read_bytes(), upload=True)
            row = api.request(f"/releases/{row['id']}")
            check_assets(row, assets_for(plan, kind), complete=True)
            row = api.request(f"/releases/{row['id']}", 'PATCH', {'draft': False, 'make_latest': 'false'})
        download_assets(plan, row, kind)
        releases[kind] = row
    report(output, 'promoting-channel', version=plan['version'])
    preflight_remote(plan)
    if remote_ref(CHANNEL) != plan['runtime']:
        # The generated commit is a direct child of baseline. The lease rejects
        # a concurrent publisher; no unrelated channel commit can be discarded.
        git('push', '--force-with-lease=' + CHANNEL + ':' + plan['baseline'],
            'origin', plan['runtime'] + ':' + CHANNEL)
    if remote_ref(CHANNEL) != plan['runtime']:
        raise ValueError('Channel changed before latest promotion; review required')
    api.request(f"/releases/{releases['installer']['id']}", 'PATCH', {'make_latest': 'true'})
    verify(output, api, plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'publish', 'verify'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source', help='Exact 40-character source SHA (prepare only)')
    parser.add_argument('--version', default='auto', help='auto increments the frozen Windows channel patch')
    parser.add_argument('--allow-publish', action='store_true')
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error('Python 3.11+ required')
    output = args.output.resolve()
    if args.action == 'publish' and not args.allow_publish:
        parser.error('publish requires --allow-publish; prepare is the offline/dry-run path')
    if args.action == 'publish' and not os.environ.get('GH_TOKEN'):
        parser.error('publish requires GH_TOKEN from existing authorized authentication')
    if args.action == 'prepare' and not re.fullmatch(r'[a-f0-9]{40}', args.source or ''):
        parser.error('prepare requires --source with an exact 40-character SHA')
    with locked(output):
        try:
            if args.action == 'prepare':
                prepare(output, args.source, args.version)
            else:
                plan = load_bundle(output)
                if args.action == 'publish':
                    publish(output, GitHub(), plan)
                else:
                    verify(output, GitHub(), plan)
        except Exception as error:
            # Keep the last completed phase plus a safe failure category. Re-run
            # the same command; remote state, not this journal, decides recovery.
            previous = json.loads((output / 'progress.json').read_bytes()) if (output / 'progress.json').exists() else {}
            report(output, 'failed', previous_phase=previous.get('phase'), error_type=type(error).__name__)
            if isinstance(error, ValueError):
                print(str(error), file=sys.stderr)
            else:
                print('Operation unconfirmed; inspect remote state and retry the same bundle', file=sys.stderr)
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
