"""Real Windows API/transaction tests with only synthetic private state."""
import copy
import ctypes
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / n) for n in ('scripts', 'adapters/codex', 'tools', 'tests')]
from brain_lock import private_directory, private_mode, locked, LockTimeout, command_line, native_platform


@unittest.skipUnless(os.name == 'nt', 'Native Windows APIs required')
class WindowsNative(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        from brain_windows import protect_new
        protect_new(self.root, directory=True)
        self.home = private_directory(self.root / 'User Space & 文本')

    def test_credentials_native_roundtrip_cas_size_and_removal(self):
        from brain_windows import WindowsCredentials
        from system_credentials import CredentialStore
        backend = WindowsCredentials('agent-brain.https.' + secrets.token_hex(32))
        store = CredentialStore(backend, self.home / 'credentials')
        record = {'schema': 1, 'fixture': 'synthetic-not-a-device-token', 'phase': 'preparing'}
        with store.lease():
            self.assertIsNone(store.load())
            store.save(record, None)
            try:
                self.assertEqual(store.load(), record)
                with self.assertRaises(ValueError): store.save({'changed': True}, None)
                with self.assertRaises(ValueError): store.save({'fixture': 'x' * 3000}, record)
                self.assertEqual(store.load(), record)
            finally: store.clear(record)
            self.assertIsNone(store.load())
        private_mode(self.home / 'credentials', directory=True, owner=True)

    def test_dacl_rejects_other_principal_without_repairing_existing_item(self):
        from brain_windows import libraries, user_sid
        from ctypes import wintypes as W
        file = self.home / 'synthetic.txt'; file.write_text('Synthetic public fixture')
        api, kernel = libraries(); descriptor = ctypes.c_void_p()
        api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [W.LPCWSTR, W.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        api.SetFileSecurityW.argtypes = [W.LPCWSTR, W.DWORD, ctypes.c_void_p]
        self.assertTrue(api.ConvertStringSecurityDescriptorToSecurityDescriptorW('O:' + user_sid() + 'D:P(A;;FA;;;' + user_sid() + ')(A;;FR;;;WD)', 1, ctypes.byref(descriptor), None))
        try: self.assertTrue(api.SetFileSecurityW(str(file), 4 | 0x80000000, descriptor))
        finally: kernel.LocalFree(descriptor)
        with self.assertRaises(ValueError): private_mode(file, owner=True)
        self.assertEqual(file.read_text(), 'Synthetic public fixture')

    def test_shared_worker_lease_survives_launcher_and_excludes_update(self):
        file = self.home / 'worker.lock'
        code = "import sys;sys.path.insert(0,sys.argv[1]);from brain_lock import locked\nwith locked(sys.argv[2],shared=True):\n print('ready',flush=True);sys.stdin.readline()"
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code, str(ROOT / 'scripts'), str(file)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            with locked(file, timeout=0, shared=True): pass
            with self.assertRaises(LockTimeout):
                with locked(file, timeout=0): pass
        finally:
            child.stdin.close(); child.wait(timeout=10); child.stdout.close()
        with locked(file, timeout=1): pass

    def test_hook_command_preserves_spaces_shell_symbols_unicode_and_quotes(self):
        expected = 'literal %PATH% $env:PATH & 文本 "quote"'
        command = command_line([sys.executable, '-I', '-B', '-c', 'print(' + repr(expected) + ')'])
        self.assertLess(len(command), 8191)
        run = subprocess.run(command, shell=True, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), expected)

    def test_real_shared_installer_and_owned_global_hook_transaction(self):
        import mac_first_install as installer
        import content_install
        import global_memory_install as hooks
        import global_memory
        from types import SimpleNamespace
        native = private_directory(self.home / 'codex')
        config = native / 'config.toml'; config.write_text('model="synthetic"\n', encoding='utf-8')
        connection = {'schema': 1, 'endpoint': 'https://synthetic.invalid',
            'grant': {'adapter': 'codex', 'platform': native_platform(), 'actor': 'synthetic-owner', 'scopes': ['read','put'],
                      'artifact_types': ['memory','rule','skill'], 'ids': {k:'personal-owner' for k in ('memory','rule','skill')}},
            'release': {'origin': installer.OFFICIAL_ORIGIN, 'ref': 'refs/heads/releases/personal-https-windows-stable', 'anchor_commit': 'a'*40}}
        materials = installer.prepare_dependencies(self.home)
        from unittest.mock import patch
        from build_client import release
        from client_release import ClientReleases
        payload = self.root / 'synthetic-release'
        release(ROOT, payload, '0.5.0', data_mode='personal-content-https', target_platform=native_platform())
        manifest = json.loads((payload / 'releases/manifest.json').read_bytes())
        class Source:
            def latest(self):
                return 'a'*40, manifest, lambda name: (payload / name).read_bytes()
        def update():
            state = self.home / '.config/agent-brain-client/first-install'
            policy = state / 'release-policy.json'
            client = ClientReleases(policy, installer.sha(policy.read_bytes()))
            client.engine.source = Source()
            return client.update()
        with patch.dict(os.environ, CODEX_HOME=str(native)):
            plan, pin, _ = installer.prepare_materials(materials, self.home, native, connection,
                installer.sha(installer.canonical(connection)), updater=update)
            result = content_install.apply(plan, pin, lambda value: None)
            self.assertTrue(result['installed'], result)
            inspected = content_install.inspect(self.home)
            self.assertTrue(inspected.get('registration_verified'), inspected)
            again = content_install.apply(plan, pin, lambda value: None)
            self.assertTrue(again['installed'])
            args = SimpleNamespace(device_home=str(self.home), operator=None, operator_sha256=None, profile_id='f'*32)
            hooks.install(args)
            value = global_memory.load(self.home)
            text = config.read_text(encoding='utf-8')
            self.assertIn('commandWindows', text)
            self.assertEqual(value['legacy_projects'], [])
            self.assertFalse(global_memory.approved(value))
            hooks.uninstall(args)
            self.assertNotIn(hooks.BEGIN, config.read_text(encoding='utf-8'))
            self.assertTrue(content_install.inspect(self.home)['registration_verified'])

    def test_offline_outbox_reconnect_and_rule_skill_mapping_share_same_core(self):
        from test_content_outbox import Cloud, POLICY, MEMORY, RULE, SKILL
        from content_cache import ContentAdapter
        cloud = Cloud(); cloud.grant.update(platform=native_platform())
        cache = self.home / 'cache'
        def adapter(): return ContentAdapter(self.home, cache, copy.deepcopy(POLICY), cloud, codex_home=self.home / 'codex')
        first = adapter(); cloud.offline = True
        request = {'request_id':'synthetic_windows_write_001','method':'content_put',
                   'params':{'kind':'memory','id':'synthetic-topic','record':MEMORY,'expected_revision':'missing'}}
        self.assertTrue(first.invoke(request)['local_queue']['saved'])
        cloud.offline = False
        cloud.records.update({('rule','synthetic-rule'):RULE, ('skill','synthetic-skill'):SKILL})
        self.assertEqual(adapter().refresh()['status'], 'content-refreshed')
        self.assertEqual(cloud.puts, 1)
        self.assertEqual(adapter().queue_status()['confirmed'], 1)
        private_mode(cache / 'active.json', owner=True)
        self.assertEqual([r for r in cloud.calls if r['method']=='content_put'], [request, request])


if __name__ == '__main__': unittest.main()
