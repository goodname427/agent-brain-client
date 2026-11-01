"""Run the installer from a clean, separately downloaded official main tree.

This is deliberately a small checkout bootstrap, not a self-updater for the
installed runtime. It never edits or falls back to the caller's checkout.
"""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ORIGIN = 'https://github.com/goodname427/agent-brain-client.git'
BRANCH = 'main'
SOURCE_RECORD = 'agent-brain-source-sha'
ONE_SHOT = 'agent-brain-bootstrap-once'


def _run(args, *, runner, env):
    return runner(args, capture_output=True, text=True, timeout=120, env=env)


def should_bootstrap(argv):
    """Refresh only the default install/reinstall entry, never local maintenance."""
    args = list(argv)
    action = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == '--action':
            if index + 1 >= len(args):
                return False
            action = args[index + 1]
            index += 2
            continue
        if arg.startswith('--action='):
            action = arg.split('=', 1)[1]
        index += 1
    return action in (None, 'install')


def verified_one_shot(root, *, runner=subprocess.run, git='git'):
    root = Path(root).resolve()
    git_dir = root / '.git'
    record = git_dir / SOURCE_RECORD
    marker = git_dir / ONE_SHOT
    if not git_dir.is_dir() or not record.is_file() or not marker.is_file():
        return False
    commit = record.read_text().strip()
    if not re.fullmatch(r'[a-f0-9]{40}', commit) or marker.read_text().strip() != commit:
        return False
    env = {'PATH': os.environ.get('PATH', '') if os.name == 'nt' else '/usr/bin:/bin', 'HOME': os.environ.get('HOME', ''),
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_TERMINAL_PROMPT': '0'}
    if os.name == 'nt': env.update(SystemRoot=os.environ['SystemRoot'], USERPROFILE=os.environ['USERPROFILE'])
    head = _run([git, '-C', str(root), 'rev-parse', 'HEAD'], runner=runner, env=env)
    branch = _run([git, '-C', str(root), 'branch', '--show-current'], runner=runner, env=env)
    origin = _run([git, '-C', str(root), 'remote', 'get-url', 'origin'], runner=runner, env=env)
    live_main = _run([git, '-C', str(root), 'ls-remote', '--exit-code', 'origin',
                       'refs/heads/main'], runner=runner, env=env)
    live_commit = live_main.stdout.split()[0] if live_main.stdout.split() else ''
    status = _run([git, '-C', str(root), 'status', '--porcelain', '--untracked-files=all'], runner=runner, env=env)
    if (head.returncode or head.stdout.strip() != commit or branch.returncode
            or branch.stdout.strip() != BRANCH or origin.returncode
            or origin.stdout.strip() != ORIGIN or live_main.returncode
            or live_commit != commit or status.returncode or status.stdout.strip()):
        raise ValueError('Fresh official checkout changed; preserve it and retry from a clean checkout')
    marker.unlink()
    return True


def fresh_checkout(home, *, runner=subprocess.run, git='git', root=None):
    base = Path(root) if root is not None else Path(home) / ('.config/agent-brain-client/installer-checkouts' if os.name == 'nt' else 'Library/Application Support/AgentBrain/installer-checkouts')
    if any(path.is_symlink() for path in (base, *base.parents)):
        raise ValueError('Canonical private checkout directory required')
    if os.name == 'nt':
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        from brain_lock import private_directory
        private_directory(base)
    else:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        st = base.stat()
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ValueError('Private owned checkout directory required')
    checkout = Path(tempfile.mkdtemp(prefix='main-', dir=base))
    env = {'PATH': os.environ.get('PATH', '') if os.name == 'nt' else '/usr/bin:/bin', 'HOME': str(home),
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_TERMINAL_PROMPT': '0'}
    if os.name == 'nt': env.update(SystemRoot=os.environ['SystemRoot'], USERPROFILE=str(home))
    cloned = _run([git, 'clone', '--depth', '1', '--single-branch', '--branch', BRANCH,
                   '--no-tags', '--', ORIGIN, str(checkout)], runner=runner, env=env)
    if cloned.returncode:
        raise ValueError('Official main checkout download failed; caller checkout was not run')
    head = _run([git, '-C', str(checkout), 'rev-parse', 'HEAD'], runner=runner, env=env)
    branch = _run([git, '-C', str(checkout), 'branch', '--show-current'], runner=runner, env=env)
    origin = _run([git, '-C', str(checkout), 'remote', 'get-url', 'origin'], runner=runner, env=env)
    status = _run([git, '-C', str(checkout), 'status', '--porcelain', '--untracked-files=all'], runner=runner, env=env)
    commit = head.stdout.strip()
    if (head.returncode or not re.fullmatch(r'[a-f0-9]{40}', commit)
            or branch.returncode or branch.stdout.strip() != BRANCH
            or origin.returncode or origin.stdout.strip() != ORIGIN
            or status.returncode or status.stdout.strip()):
        raise ValueError('Downloaded official main checkout failed validation; caller checkout was not run')
    git_dir = checkout / '.git'
    (git_dir / SOURCE_RECORD).write_text(commit + '\n')
    (git_dir / SOURCE_RECORD).chmod(0o600)
    (git_dir / ONE_SHOT).write_text(commit + '\n')
    (git_dir / ONE_SHOT).chmod(0o600)
    print('Official main commit: ' + commit, file=sys.stderr)
    print('Independent installer checkout: ' + str(checkout), file=sys.stderr)
    return checkout


def ensure_checkout(root, *, home=None, runner=subprocess.run, git='git', base=None):
    root = Path(root).resolve()
    if verified_one_shot(root, runner=runner, git=git):
        return root
    return fresh_checkout(home or Path.home(), runner=runner, git=git, root=base)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == '--should-bootstrap':
        return 0 if should_bootstrap(argv[1:]) else 1
    if argv != ['--ensure']:
        return 2
    try:
        root = Path(__file__).resolve().parents[1]
        checkout = ensure_checkout(root)
        print(checkout)
        return 0
    except Exception:
        print('Official installer checkout unavailable; no older checkout was run.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
