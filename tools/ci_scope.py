"""Conservative Client CI selection using local Git, never API file-list limits."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MAC = frozenset({
    'native/AccessCredentialStore.swift', 'native/CredentialStore.swift',
    'native/DeviceComponent.swift', 'tools/build_credential_helper.py',
    'tools/install-mac.sh', 'tools/memory.sh',
})
WINDOWS = frozenset({
    'scripts/brain_windows.py', 'tools/access_windows.py',
    'tools/install-windows.ps1', 'tools/memory-windows.ps1',
    'tools/windows-python.ps1', 'tests/test_windows_native.py',
})
SHA = re.compile(r'[0-9a-f]{40}')


def selection(mac=True, windows=True, reason='conservative fallback'):
    return dict(code=mac or windows, mac=mac, windows=windows,
                full=mac and windows, reason=reason)


def classify(paths):
    mac = windows = False
    if not paths:
        return selection(reason='empty or unavailable change set')
    for name in paths:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or str(path) != name:
            return selection(reason='unrecognized path')
        # Keep the allowlists narrow. A new extension/file defaults to both OSes.
        if name.endswith('.md') and (len(path.parts) == 1 or path.parts[0] == 'docs'):
            continue
        if name in MAC:
            mac = True
        elif name in WINDOWS:
            windows = True
        else:
            return selection(reason='shared code, configuration or unknown path')
    return selection(mac, windows, 'union of changed paths')


def git(*args, root=ROOT):
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True,
                          timeout=60).stdout


def changed_paths(base, head, root=ROOT):
    """Disable rename detection so both old and new names contribute scope.

    Raw NUL records preserve whitespace/newlines and expose symlinks/submodules.
    """
    raw = git('diff', '--raw', '--no-abbrev', '--no-renames', '-z', base, head,
              '--', root=root)
    fields = raw.split(b'\0')
    if fields[-1] != b'' or (len(fields) - 1) % 2:
        raise ValueError('Malformed Git diff')
    paths = []
    for index in range(0, len(fields) - 1, 2):
        old, new, _, _, status = fields[index].decode('ascii').split()
        if old[0] != ':' or status not in ('A', 'M', 'D', 'T'):
            raise ValueError('Unexpected Git record')
        if old[1:] not in ('000000', '100644', '100755') or new not in ('000000', '100644', '100755'):
            raise ValueError('Linked or external change')
        name = fields[index + 1].decode('utf-8')
        # Root Markdown is part of fixed package/entrypoint file sets. Text-only
        # edits remain documentation-only; structural changes need build coverage.
        if name.endswith('.md') and '/' not in name and status in ('A', 'D'):
            raise ValueError('Root documentation file set changed')
        if name.endswith('.md') and any(mode not in ('000000', '100644') for mode in (old[1:], new)):
            raise ValueError('Executable documentation change')
        paths.append(name)
    return paths


def plan_event(event_name, event, root=ROOT):
    if event_name in ('workflow_dispatch', 'merge_group'):
        return selection(reason='manual or merge queue full validation')
    try:
        if event_name == 'pull_request':
            base = event['pull_request']['base']['sha']
            head = event['pull_request']['head']['sha']
            if not SHA.fullmatch(base) or not SHA.fullmatch(head):
                raise ValueError('Missing PR commits')
            base = git('merge-base', base, head, root=root).decode().strip()
        elif event_name == 'push':
            base, head = event['before'], event['after']
            if not SHA.fullmatch(base) or not SHA.fullmatch(head) or base == '0' * 40:
                raise ValueError('Missing push commits')
            git('merge-base', '--is-ancestor', base, head, root=root)
        else:
            raise ValueError('Unknown event')
        return classify(changed_paths(base, head, root))
    except (KeyError, TypeError, ValueError, UnicodeError, OSError, subprocess.SubprocessError):
        return selection(reason='base/diff unavailable or unrecognized; running all checks')


def verify_results(needs):
    """A required check fails on missing outputs, failed selection or cancelled jobs."""
    for job in ('changes', 'documentation'):
        if needs[job]['result'] != 'success':
            raise ValueError(job + ' did not succeed')
    outputs = needs['changes']['outputs']
    for flag, job in (('code', 'source-checks'), ('mac', 'isolated-client'),
                      ('windows', 'windows-contracts')):
        if outputs[flag] not in ('true', 'false'):
            raise ValueError('Missing selection: ' + flag)
        expected = 'success' if outputs[flag] == 'true' else 'skipped'
        if needs[job]['result'] != expected:
            raise ValueError(job + ': expected ' + expected)
    expected_full = str(outputs['mac'] == outputs['windows'] == 'true').lower()
    if outputs['full'] != expected_full or outputs['code'] != str('true' in (outputs['mac'], outputs['windows'])).lower():
        raise ValueError('Inconsistent selection')


def main():
    if len(sys.argv) == 2 and sys.argv[1] == 'verify':
        verify_results(json.loads(os.environ['CI_NEEDS']))
        print('Selected checks succeeded. Skipped platforms were not validated.')
        return
    with open(os.environ['GITHUB_EVENT_PATH'], encoding='utf-8') as source:
        event = json.load(source)
    plan = plan_event(os.environ['GITHUB_EVENT_NAME'], event)
    with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
        for key in ('code', 'mac', 'windows', 'full'):
            output.write(f'{key}={str(plan[key]).lower()}\n')
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
        summary.write('Client CI scope (false means not validated):\n\n```json\n'
                      + json.dumps(plan, indent=2) + '\n```\n')
    print(json.dumps(plan))


if __name__ == '__main__':
    main()
