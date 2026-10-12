"""Portable paths and pairing seams; synthetic Windows is not device support."""
from contextlib import nullcontext
import copy
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / n) for n in ('tools', 'scripts', 'adapters/codex')]
from client_release import runtime_environment, UpdateBlocked
from https_content import DeviceEnrollment, grant_check, native_store
import prepare_install_runtime as bootstrap


class Contracts(unittest.TestCase):
    def test_windows_paths_spaces_drive_separators_and_no_ambient_environment(self):
        value = runtime_environment(r'C:\Program Files\Python\python.exe',
                                    r'D:\Git Tools\bin\git.exe', r'D:\Git Tools\libexec', windows=True)
        self.assertEqual(value['PATH'], r'C:\Program Files\Python;D:\Git Tools\bin')
        self.assertEqual(value['GIT_EXEC_PATH'], r'D:\Git Tools\libexec')
        self.assertEqual(value['GIT_CONFIG_GLOBAL'], 'NUL')
        self.assertEqual(value['GIT_TERMINAL_PROMPT'], '0')
        self.assertNotIn('PYTHONPATH', value)
        for path in (r'C:relative\python.exe', r'C:\tools\..\python.exe', '/relative'):
            with self.assertRaises(UpdateBlocked):
                runtime_environment(path, r'C:\git\git.exe', r'C:\git\core', windows=True)

    def test_posix_paths_keep_spaces_and_distinct_separator(self):
        value = runtime_environment('/opt/Python Tools/python3', '/opt/git/bin/git', '/opt/git/core')
        self.assertEqual(value['PATH'], '/opt/Python Tools:/opt/git/bin')
        self.assertEqual(value['GIT_CONFIG_GLOBAL'], '/dev/null')

    def test_windows_contract_recovery_reuses_input_and_nonce_without_native_backend(self):
        grant = {'adapter': 'codex', 'platform': 'win32-amd64', 'actor': 'synthetic',
                 'scopes': ['read', 'put'], 'artifact_types': ['memory'], 'ids': {'memory': '*'}}
        profile = {'grant': grant}
        class Store:
            record = None
            def lease(self): return nullcontext()
            def load(self): return copy.deepcopy(self.record)
            def save(self, value, expected):
                if expected != self.record:
                    raise ValueError('SYNTHETIC_CAS')
                self.record = copy.deepcopy(value)
        class Transport:
            seen = []
            lost = True
            def prepare(self, code, nonce, profile):
                self.seen.append((code, nonce))
                if self.lost:
                    self.lost = False
                    raise OSError('SYNTHETIC_LOST_REPLY')
                return {'phase': 'pending', 'nonce': nonce, 'credential': 't' * 40,
                        'device_id': 'd' * 32, 'grant': profile['grant']}
            def confirm(self, credential, nonce):
                return {'phase': 'active', 'device_id': 'd' * 32, 'grant': grant}
        store, transport = Store(), Transport()
        calls = []
        enrollment = DeviceEnrollment(profile, 'a' * 64, store, transport,
                                      lambda: calls.append(True) or 'i' * 40)
        with patch('https_content.NativeKeychain', side_effect=AssertionError('No Mac backend in shared contract')):
            self.assertEqual(enrollment.run()['status'], 'pairing-resume-required')
            self.assertEqual(enrollment.run()['status'], 'device-paired')
        self.assertEqual(calls, [True])
        self.assertEqual(transport.seen[0], transport.seen[1])
        self.assertNotIn('code', store.record)
        # A synthetic injectable contract does not grant Windows production use.
        with self.assertRaises(ValueError):
            grant_check(grant)
        with patch('sys.platform', 'win32'):
            with self.assertRaises(ValueError):
                native_store({'credential_helper': 'synthetic.exe', 'credential_helper_sha256': 'a' * 64,
                              'state_directory': 'synthetic'}, 'a' * 64).backend.secure_input()


@unittest.skipIf(sys.platform == 'win32', 'Mac bootstrap provisioning only; portable contracts above run on Windows')
class Bootstrap(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()

    def archive(self, path, *, malicious=False):
        with tarfile.open(path, 'w:gz') as out:
            entry = tarfile.TarInfo('python/bin/python3')
            if malicious:
                entry.type = tarfile.SYMTYPE
                entry.linkname = '../../../outside'
                out.addfile(entry)
            else:
                entry.mode = 0o700
                content = b'SYNTHETIC_UNEXECUTED_RUNTIME'
                entry.size = len(content)
                out.addfile(entry, io.BytesIO(content))
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_python_311_reuses_current_interpreter_without_download(self):
        with patch.object(bootstrap.sys, 'version_info', (3, 11)):
            self.assertEqual(bootstrap.runtime(self.home, fetch=lambda p: self.fail('No download')),
                             Path(sys.executable).resolve())
        self.assertFalse((self.home / '.config').exists())

    def test_verified_archive_preparation_reuses_runtime_and_retains_failed_staging(self):
        archive = self.home / 'synthetic.tar.gz'
        digest = self.archive(archive)
        calls = []
        def fetch(path):
            calls.append(True)
            path.write_bytes(archive.read_bytes())
        with patch.object(bootstrap.sys, 'version_info', (3, 9)), patch.object(bootstrap, 'DIGEST', digest):
            first = bootstrap.runtime(self.home, fetch=fetch)
            self.assertEqual(first.read_bytes(), b'SYNTHETIC_UNEXECUTED_RUNTIME')
            self.assertEqual(bootstrap.runtime(self.home, fetch=fetch), first)
            self.assertEqual(calls, [True])
            first.write_bytes(b'SYNTHETIC_DRIFT')
            with self.assertRaises(ValueError):
                bootstrap.runtime(self.home, fetch=fetch)

    def test_bad_digest_and_download_failure_never_execute_or_activate(self):
        with patch.object(bootstrap.sys, 'version_info', (3, 9)):
            with self.assertRaises(ValueError):
                bootstrap.runtime(self.home, fetch=lambda p: p.write_bytes(b'SYNTHETIC_CORRUPTION'))
            def unavailable(path): raise OSError('SYNTHETIC_OFFLINE')
            with self.assertRaises(OSError):
                bootstrap.runtime(self.home, fetch=unavailable)
        directory = self.home / '.config/agent-brain-client/install-runtime'
        self.assertFalse((directory / bootstrap.DIGEST).exists())
        self.assertTrue(list(directory.glob('preparing-*/python.tar.gz')))

    def test_verified_but_escaping_archive_is_rejected(self):
        archive = self.home / 'synthetic.tar.gz'
        digest = self.archive(archive, malicious=True)
        with patch.object(bootstrap, 'DIGEST', digest):
            with self.assertRaises(ValueError):
                bootstrap.extract(archive, self.home / 'out')
        self.assertFalse((self.home / 'out').exists())


if __name__ == '__main__':
    unittest.main()
