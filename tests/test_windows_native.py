"""Real Windows API/transaction tests with only synthetic private state."""
import copy
import ctypes
import json
import os
from pathlib import Path
import secrets
import shutil
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

    def test_native_platform_in_minimal_environment_and_forged_processor_variables(self):
        code = "import sys;sys.path.insert(0,sys.argv[1]);from brain_lock import require_platform;print(require_platform('minimal-environment'))"
        for extra in ({}, {'PROCESSOR_ARCHITECTURE': 'x86', 'PROCESSOR_ARCHITEW6432': 'unsupported'}):
            environment = dict(extra, SystemRoot=os.environ['SystemRoot'])
            run = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(ROOT / 'scripts')],
                env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), native_platform())

    def test_python_selector_skips_store_alias_and_broken_candidate(self):
        selector = ROOT / 'tools/windows-python.ps1'
        alias = self.home / 'Microsoft/WindowsApps/python.exe'
        alias.parent.mkdir(parents=True)
        alias.write_bytes(b'SYNTHETIC_ALIAS_MUST_NOT_EXECUTE')
        broken = self.home / 'broken.exe'
        broken.write_bytes(b'SYNTHETIC_NOT_AN_EXECUTABLE')
        for shell in ('powershell.exe', 'pwsh.exe'):
            if not shutil.which(shell):
                continue
            def literal(value): return "'" + str(value).replace("'", "''") + "'"
            script = ". " + literal(selector) + "; Resolve-AgentBrainPython -Candidates @(" + ','.join(
                '@{Command=' + literal(p) + ';Prefix=@()}' for p in (alias, broken, sys.executable)) + ')'
            run = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', script], capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(Path(run.stdout.strip()).resolve(), Path(sys.executable).resolve())

    def test_enrolled_pre_native_050_recovers_to_verified_release_without_new_input(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest.mock import patch
        import mac_first_install as wizard
        import content_install
        from build_client import export, release
        from client_release import ClientReleases
        from test_https_content import Backend
        native = private_directory(self.home / 'codex')
        config = native / 'config.toml'
        config.write_text('model="synthetic"\n', encoding='utf-8')
        initial_config = config.read_bytes()
        target = native_platform()
        connection = {'schema': 1, 'endpoint': 'https://synthetic.invalid',
            'grant': {'adapter': 'codex', 'platform': target, 'actor': 'synthetic-owner', 'scopes': ['read', 'put'],
                'artifact_types': ['memory', 'rule', 'skill'], 'ids': {k: 'personal-owner' for k in ('memory', 'rule', 'skill')}},
            'release': {'origin': wizard.OFFICIAL_ORIGIN, 'ref': 'refs/heads/releases/personal-https-windows-stable' if target.startswith('win32-') else wizard.OFFICIAL_REF, 'anchor_commit': 'a' * 40}}
        materials = wizard.prepare_dependencies(self.home)
        old_detector = (ROOT / 'tests/fixtures/windows_platform_050/brain_lock.py.fixture').read_bytes()
        def legacy_package(path):
            (path / 'scripts/brain_lock.py').write_bytes(old_detector)
            for name in ('export-manifest.json', 'releases/manifest.json'):
                file = path / name
                if file.exists():
                    manifest = json.loads(file.read_bytes())
                    manifest['files']['scripts/brain_lock.py'] = wizard.sha(old_detector)
                    file.write_text(json.dumps(manifest), encoding='utf-8')
        payloads = {}
        for version in ('0.5.0', '0.5.3'):
            payload = self.root / version
            release(ROOT, payload, version, data_mode='personal-content-https', target_platform=target)
            if version == '0.5.0': legacy_package(payload)
            payloads[version] = payload
        def updater(version):
            payload = payloads[version]
            manifest = json.loads((payload / 'releases/manifest.json').read_bytes())
            class Source:
                def latest(self):
                    return ('a' if version == '0.5.0' else 'b') * 40, manifest, lambda name: (payload / name).read_bytes()
            def run():
                policy = self.home / '.config/agent-brain-client/first-install/release-policy.json'
                releases = ClientReleases(policy, wizard.sha(policy.read_bytes()))
                releases.engine.source = Source()
                result = releases.engine.check()
                return dict(result, active=releases.verified_active())
            return run
        backend = Backend()
        store = SimpleNamespace(load=backend.load, save=backend.save, lease=nullcontext)
        grant = connection['grant']
        peer = SimpleNamespace(
            prepare=lambda code, nonce, profile: {'phase': 'pending', 'nonce': nonce, 'credential': 't' * 40, 'device_id': 'd' * 32, 'grant': grant},
            confirm=lambda token, nonce: {'phase': 'active', 'device_id': 'd' * 32, 'grant': grant})
        def old_export(source, output, **options):
            result = export(source, output, **options)
            legacy_package(output)
            return result
        options = dict(access_mode=True, dependency_preparer=lambda *a, **k: materials, store=store, transport=peer,
            confirm=lambda *a: self.fail('No invitation prompt'), secure_input=lambda: 'SYNTHETIC_ENROLLMENT_MARKER_12345678901234567890')
        with patch.object(wizard, 'export', side_effect=old_export):
            first = wizard.run(self.home, native, connection, wizard.sha(wizard.canonical(connection)), updater=updater('0.5.0'), **options)
        self.assertFalse(first['installed'], first)
        self.assertEqual(backend.record['phase'], 'active')
        identity = copy.deepcopy(backend.record)
        self.assertEqual(config.read_bytes(), initial_config)
        state = self.home / '.config/agent-brain-client/first-install'
        old_plan = (state / 'install-plan.json').read_bytes()
        old_bootstrap = state / 'bootstrap'
        self.assertEqual((old_bootstrap / 'scripts/brain_lock.py').read_bytes(), old_detector)
        options['secure_input'] = lambda: self.fail('Recovery must not request a new credential')
        def offline_bootstrap_probe(plan):
            environment = dict(plan['mcp']['env'], HOME=plan['device_home'])
            run = subprocess.run([plan['mcp']['command'], *plan['mcp']['args'], '--bootstrap-check'],
                env=environment, capture_output=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)['active_package_version'], '0.5.3')
        options['installer'] = lambda plan, digest: content_install.apply(plan, digest, offline_bootstrap_probe)
        import pending_install_recovery as recovery
        original_save = recovery.save
        def interrupted(path, value):
            original_save(path, value)
            if Path(path).name == 'install-plan.json':
                raise OSError('SYNTHETIC_PLAN_PIN_INTERRUPTION')
        with patch.object(recovery, 'save', side_effect=interrupted):
            interrupted_result = wizard.run(self.home, native, connection, wizard.sha(wizard.canonical(connection)), updater=updater('0.5.3'), **options)
        self.assertFalse(interrupted_result['installed'])
        self.assertEqual(config.read_bytes(), initial_config)
        self.assertEqual(backend.record, identity)
        second = wizard.run(self.home, native, connection, wizard.sha(wizard.canonical(connection)),
            updater=lambda: self.fail('Interrupted recovery reuses verified package'), **options)
        self.assertTrue(second['installed'], second)
        self.assertEqual(backend.record, identity)
        journal = content_install.private_json(state / 'platform-recovery.json')
        self.assertEqual(journal['phase'], 'complete')
        self.assertEqual(journal['before'], json.loads(old_plan))
        self.assertEqual((old_bootstrap / 'scripts/brain_lock.py').read_bytes(), old_detector)
        self.assertTrue(content_install.inspect(self.home)['installed'])

    def test_exact_crlf_pre_enrollment_metadata_recovers_without_resetting_dependencies(self):
        from unittest.mock import patch
        import mac_first_install as wizard
        import pending_install_recovery as recovery
        native = private_directory(self.home / 'codex')
        connection = {'schema': 1, 'endpoint': 'https://synthetic.invalid',
            'grant': {'adapter': 'codex', 'platform': native_platform(), 'actor': 'synthetic-owner', 'scopes': ['read', 'put'],
                'artifact_types': ['memory', 'rule', 'skill'], 'ids': {k: 'personal-owner' for k in ('memory', 'rule', 'skill')}},
            'release': {'origin': wizard.OFFICIAL_ORIGIN, 'ref': 'refs/heads/releases/personal-https-windows-stable', 'anchor_commit': 'a' * 40}}
        materials = wizard.prepare_dependencies(self.home)
        crlf = copy.deepcopy(materials)
        crlf['source_sha256'] = wizard.sha((ROOT / 'scripts/brain_windows.py').read_bytes().replace(b'\n', b'\r\n'))
        root = self.home / '.config/agent-brain-client'
        wizard.same_private(root / 'access-bootstrap/connection.json', connection)
        with self.assertRaises(ValueError):
            wizard.prepare_materials(crlf, self.home, native, connection, wizard.sha(wizard.canonical(connection)),
                updater=lambda: {'status': 'offline', 'active': None})
        state = root / 'first-install'
        before = {name: (state / name).read_bytes() for name in ('binding.json', 'https-profile.json', 'release-policy.json', 'dependencies.json', 'installation-id.json')}
        # An interruption after one metadata replacement resumes from preserved pins.
        original_save = recovery.save
        def interrupted(path, value):
            original_save(path, value)
            if Path(path).name == 'binding.json': raise OSError('SYNTHETIC_INTERRUPTION')
        with patch.object(recovery, 'save', side_effect=interrupted), self.assertRaises(OSError):
            recovery.repair_crlf_preparation(self.home, native)
        recovery.repair_crlf_preparation(self.home, native)
        self.assertEqual((state / 'dependencies.json').read_bytes(), before['dependencies.json'])
        self.assertEqual((state / 'installation-id.json').read_bytes(), before['installation-id.json'])
        journal = wizard.private_json(state / 'crlf-recovery.json')
        self.assertEqual(journal['phase'], 'complete')
        self.assertEqual(journal['before']['https-profile.json'], json.loads(before['https-profile.json']))
        self.assertEqual(wizard.private_json(state / 'https-profile.json')['credential_backend']['source_sha256'], materials['source_sha256'])
        self.assertFalse((root / 'https').exists())

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
            result = client.engine.check()
            result['active'] = client.verified_active()
            return result
        with patch.dict(os.environ, CODEX_HOME=str(native)):
            plan, pin, _ = installer.prepare_materials(materials, self.home, native, connection,
                installer.sha(installer.canonical(connection)), updater=update)
            result = content_install.apply(plan, pin, lambda value: None)
            self.assertTrue(result['installed'], result)
            inspected = content_install.inspect(self.home)
            self.assertTrue(inspected.get('registration_verified'), inspected)
            # A process can stop after registration is written but before the
            # receipt is committed. Retry the normal no-argument CLI, preserving
            # the profile and using the existing owned journal recovery.
            state = self.home / '.config/agent-brain-client'
            receipt = content_install.private_json(state / 'content-install-receipt.json')
            receipt['phase'] = 'applying'
            content_install.save(state / 'content-install-receipt.json', receipt)
            installer.same_private(state / 'access-bootstrap/endpoint.json', {'schema': 1, 'endpoint': connection['endpoint']})
            installer.same_private(state / 'access-bootstrap/connection.json', connection)
            original_run = installer.run
            def resume(home, codex, value, digest, **options):
                return original_run(home, codex, value, digest, **options,
                    confirm=lambda *a: self.fail('No invitation or consent prompt during exact recovery'),
                    dependency_preparer=lambda *a, **k: materials,
                    secure_input=lambda: self.fail('No new credential input'),
                    updater=lambda: self.fail('No replacement release'),
                    installer=lambda p, d: content_install.apply(p, d, lambda value: None))
            import io
            with patch.object(Path, 'home', return_value=self.home), patch.object(sys, 'argv', ['mac_first_install.py']), patch.object(installer, 'run', side_effect=resume), patch.object(sys, 'stdout', io.StringIO()) as output:
                self.assertEqual(installer.main(), 0, output.getvalue())
            self.assertTrue(json.loads(output.getvalue())['installed'])
            self.assertTrue(content_install.inspect(self.home).get('registration_verified'))
            again = content_install.apply(plan, pin, lambda value: None)
            self.assertTrue(again['installed'])
            args = SimpleNamespace(device_home=str(self.home), operator=None, operator_sha256=None, profile_id='f'*32)
            hooks.install(args)
            value = global_memory.load(self.home)
            text = config.read_text(encoding='utf-8')
            self.assertIn('commandWindows', text)
            self.assertEqual(value['legacy_projects'], [])
            self.assertFalse(global_memory.approved(value))
            checkout = self.home / 'Public 工具 checkout'
            for name in ('adapters', 'scripts', 'tools'):
                shutil.copytree(ROOT / name, checkout / name)
            environment = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home), CODEX_HOME=str(native))
            for shell in ('powershell.exe', 'pwsh.exe'):
                if shutil.which(shell) is None:
                    continue
                for script, arguments, field in (
                    ('install-windows.ps1', ['-Action', 'status'], 'installed'),
                    ('memory-windows.ps1', ['global-status', '--device-home', str(self.home)], 'global_entry_installed'),
                ):
                    with self.subTest(shell=shell, script=script):
                        command = [shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(checkout / 'tools' / script), *arguments]
                        run = subprocess.run(command, env=environment, capture_output=True, text=True, encoding='utf-8', timeout=60)
                        self.assertEqual(run.returncode, 0, run.stderr)
                        self.assertTrue(json.loads(run.stdout)[field], run.stdout)
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
