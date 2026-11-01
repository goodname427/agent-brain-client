"""Checkout bootstrap never edits or executes a stale local installer."""
from pathlib import Path
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import bootstrap_main


class FreshMainCheckout(unittest.TestCase):
    commit = '8d7f1c1c78a9a8afba10db486f278cb7f8234b80'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        if os.name == 'nt':
            from brain_windows import protect_new
            protect_new(self.root, directory=True)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.base = self.root / 'private-checkouts'
        self.base.mkdir(mode=0o700)
        self.old = self.root / 'old-checkout'
        self.old.mkdir()
        (self.old / 'tools').mkdir()
        self.old_script = self.old / 'tools/install-mac.sh'
        self.old_script.write_text('# user-modified old installer\n')

    def fake_git(self, args, **kwargs):
        args = list(args)
        if args[:1] == ['git'] and 'clone' in args:
            checkout = Path(args[-1])
            (checkout / '.git').mkdir()
            (checkout / 'tools').mkdir()
            (checkout / 'tools/install-mac.sh').write_text('# official main installer\n')
            return subprocess.CompletedProcess(args, 0, '', '')
        if 'rev-parse' in args:
            return subprocess.CompletedProcess(args, 0, self.commit + '\n', '')
        if 'branch' in args:
            return subprocess.CompletedProcess(args, 0, 'main\n', '')
        if 'ls-remote' in args:
            return subprocess.CompletedProcess(args, 0, self.commit + '\trefs/heads/main\n', '')
        if 'remote' in args:
            return subprocess.CompletedProcess(args, 0, bootstrap_main.ORIGIN + '\n', '')
        if 'status' in args:
            return subprocess.CompletedProcess(args, 0, '', '')
        if 'ls-tree' in args:
            checkout = Path(args[args.index('-C') + 1])
            records = []
            for item in (checkout / 'tools').iterdir():
                raw = item.read_bytes()
                oid = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
                records.append('100644 blob ' + oid + '\t' + item.relative_to(checkout).as_posix() + '\0')
            return subprocess.CompletedProcess(args, 0, ''.join(records), '')
        raise AssertionError(args)

    def test_autocrlf_checkout_bytes_are_lf_and_real_tamper_still_fails(self):
        import os
        source = self.root / 'source'
        source.mkdir()
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_AUTHOR_NAME='Synthetic', GIT_COMMITTER_NAME='Synthetic',
                   GIT_AUTHOR_EMAIL='synthetic@example.invalid', GIT_COMMITTER_EMAIL='synthetic@example.invalid')
        def git(*args):
            return subprocess.run(['git', '-C', str(source), *args], env=env, check=True, capture_output=True)
        git('init')
        (source / 'runtime.py').write_bytes(b'print("synthetic")\n')
        (source / '.gitattributes').write_bytes(b'* text=auto eol=lf\n')
        git('add', '.gitattributes', 'runtime.py')
        git('commit', '-m', 'Synthetic exact-byte fixture')
        clone = self.root / 'autocrlf-checkout'
        subprocess.run(['git', '-c', 'core.autocrlf=true', 'clone', str(source), str(clone)], env=env, check=True, capture_output=True)
        self.assertEqual((clone / 'runtime.py').read_bytes(), b'print("synthetic")\n')
        self.assertTrue(bootstrap_main.checkout_bytes(clone, runner=subprocess.run, git='git', env=env))
        (clone / 'runtime.py').write_bytes(b'print("synthetic")\r\n')
        self.assertFalse(bootstrap_main.checkout_bytes(clone, runner=subprocess.run, git='git', env=env))
        (clone / 'runtime.py').write_bytes(b'print("altered")\n')
        self.assertFalse(bootstrap_main.checkout_bytes(clone, runner=subprocess.run, git='git', env=env))

    def test_only_install_entries_bootstrap_fresh_main(self):
        for args, expected in (
            ([], True),
            (['--connection', '/tmp/connection.json'], True),
            (['--action', 'install'], True),
            (['--action=install'], True),
            (['--action', 'status'], False),
            (['--action=repair'], False),
            (['--action', 'uninstall'], False),
            (['--action', 'check-update'], False),
            (['--action=update'], False),
        ):
            with self.subTest(args=args):
                self.assertEqual(bootstrap_main.should_bootstrap(args), expected)

    def test_stale_or_unreachable_remote_main_never_reuses_one_shot_checkout(self):
        first = bootstrap_main.fresh_checkout(
            self.home, root=self.base, runner=self.fake_git
        )
        def moved_main(args, **kwargs):
            if 'ls-remote' in args:
                return subprocess.CompletedProcess(args, 0, 'f' * 40 + '\trefs/heads/main\n', '')
            return self.fake_git(args, **kwargs)
        with self.assertRaisesRegex(ValueError, 'changed'):
            bootstrap_main.ensure_checkout(first, runner=moved_main)
        self.assertTrue((first / '.git/agent-brain-bootstrap-once').exists())

        def offline_main(args, **kwargs):
            if 'ls-remote' in args:
                return subprocess.CompletedProcess(args, 128, '', 'SYNTHETIC_NETWORK_FAILURE')
            return self.fake_git(args, **kwargs)
        with self.assertRaisesRegex(ValueError, 'changed'):
            bootstrap_main.ensure_checkout(first, runner=offline_main)

    def test_modified_old_checkout_is_preserved_while_new_main_is_pinned(self):
        checkout = bootstrap_main.ensure_checkout(
            self.old, home=self.home, base=self.base, runner=self.fake_git
        )
        self.assertNotEqual(checkout, self.old.resolve())
        self.assertEqual(self.old_script.read_text(), '# user-modified old installer\n')
        self.assertEqual((checkout / 'tools/install-mac.sh').read_text(), '# official main installer\n')
        self.assertEqual((checkout / '.git/agent-brain-source-sha').read_text().strip(), self.commit)
        self.assertEqual((checkout / '.git/agent-brain-bootstrap-once').read_text().strip(), self.commit)

    def test_download_failure_does_not_return_or_execute_old_entry(self):
        calls = []
        def network_failure(args, **kwargs):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 128, '', 'SYNTHETIC_NETWORK_FAILURE')
        with self.assertRaisesRegex(ValueError, 'download failed'):
            bootstrap_main.ensure_checkout(
                self.old, home=self.home, base=self.base, runner=network_failure
            )
        self.assertEqual(len(calls), 1)
        self.assertIn('clone', calls[0])
        self.assertEqual(self.old_script.read_text(), '# user-modified old installer\n')

    def test_one_shot_marker_is_consumed_and_next_entry_downloads_main_again(self):
        first = bootstrap_main.fresh_checkout(
            self.home, root=self.base, runner=self.fake_git
        )
        self.assertEqual(bootstrap_main.ensure_checkout(first, runner=self.fake_git), first.resolve())
        self.assertFalse((first / '.git/agent-brain-bootstrap-once').exists())
        second = bootstrap_main.ensure_checkout(
            first, home=self.home, base=self.base, runner=self.fake_git
        )
        self.assertNotEqual(second, first.resolve())
        self.assertTrue((first / 'tools/install-mac.sh').exists())


if __name__ == '__main__':
    unittest.main()
