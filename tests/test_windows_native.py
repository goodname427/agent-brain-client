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


def host_security(path, information=7):
    """Test-only diagnostic; production writes never need descriptor access."""
    from brain_windows import libraries
    from ctypes import wintypes as W
    api, kernel = libraries(); size = W.DWORD()
    api.GetFileSecurityW.argtypes = [W.LPCWSTR, W.DWORD, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD)]
    api.GetFileSecurityW(str(path), information, None, 0, ctypes.byref(size))
    if not size.value: raise ValueError('Test token cannot read requested security metadata')
    raw = ctypes.create_string_buffer(size.value)
    if not api.GetFileSecurityW(str(path), information, raw, size, ctypes.byref(size)): raise ValueError('Test metadata unavailable')
    api.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, W.DWORD, W.DWORD, ctypes.POINTER(W.LPWSTR), ctypes.c_void_p]
    text = W.LPWSTR()
    if not api.ConvertSecurityDescriptorToStringSecurityDescriptorW(raw, 1, information, ctypes.byref(text), None): raise ValueError('Test metadata unavailable')
    try: return text.value
    finally: kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def check_install_contract(test, plan):
    """Real pinned launcher plus real probe; substitute only credential/transport I/O.

    The child executes the built runtime's Server, ContentAdapter and read-only
    protocol check. Its tool list is never copied from the expected plan.
    """
    from unittest.mock import patch
    import content_install
    real_run = subprocess.run
    environment = dict(plan['mcp']['env'], HOME=plan['device_home'])
    run = real_run([plan['mcp']['command'], *plan['mcp']['args'], '--bootstrap-check'],
                   env=environment, capture_output=True, timeout=30)
    test.assertEqual(run.returncode, 0, run.stderr)
    state = Path(plan['device_home']) / '.config/agent-brain-client/first-install'
    active = json.loads((state / 'releases/active.json').read_bytes())
    def protocol_command(argv, **options):
        test.assertIn('--installation-check', argv)
        return real_run([plan['mcp']['command'], '-I', '-B',
            str(ROOT / 'tests/fixtures/checkpoint_probe.py'), active['package'], '--installation-result'], **options)
    with patch.object(content_install.subprocess, 'run', side_effect=protocol_command):
        content_install.probe(plan)


@unittest.skipUnless(os.name == 'nt', 'Native Windows APIs required')
class WindowsNative(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        from brain_windows import protect_new
        protect_new(self.root, directory=True)
        self.home = private_directory(self.root / 'User Space & 文本')

    def host_acl(self, path, *, owner=None, inherited=False):
        from brain_windows import libraries, user_sid
        from ctypes import wintypes as W
        api, kernel = libraries(); descriptor = ctypes.c_void_p()
        # An inert, unresolvable SID models a Codex sandbox principal; this
        # never grants anything on a real host directory or private state.
        sandbox = 'S-1-5-21-111111111-222222222-333333333-1002'
        flags = 'OICI' if Path(path).is_dir() else ''
        sddl = 'O:' + (owner or user_sid()) + 'D:' + ('' if inherited else 'P') + '(A;' + flags + ';FA;;;' + user_sid() + ')(A;' + flags + ';FR;;;' + sandbox + ')'
        api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [W.LPCWSTR, W.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        api.SetFileSecurityW.argtypes = [W.LPCWSTR, W.DWORD, ctypes.c_void_p]
        self.assertTrue(api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None))
        try:
            if not api.SetFileSecurityW(str(path), 5 | (0x20000000 if inherited else 0x80000000), descriptor):
                if owner: self.skipTest('Synthetic administrator owner cannot be assigned by this token')
                self.fail('Synthetic host ACL setup failed')
        finally: kernel.LocalFree(descriptor)
        return host_security(path)

    def test_host_directory_sandbox_acl_and_administrator_owner_are_not_private(self):
        from codex_connect import host_atomic
        native = self.home / 'host-codex'; native.mkdir()
        original = self.host_acl(native)
        with self.assertRaises(ValueError): private_directory(native)
        path = native / 'config.toml'
        host_atomic(path, b'model="synthetic"\n', 0o600, None)
        security = host_security(path)
        with self.assertRaises(ValueError): private_mode(path, owner=True)
        host_atomic(path, b'model="updated"\n', 0o600, path.read_bytes())
        self.assertEqual(host_security(path), security)
        self.assertEqual(host_security(native), original)

    def test_administrator_owned_host_directory_and_file_keep_security(self):
        from codex_connect import host_atomic
        admin = self.home / 'admin-codex'; admin.mkdir()
        admin_security = self.host_acl(admin, owner='BA')
        with self.assertRaises(ValueError): private_directory(admin)
        host_atomic(admin / 'config.toml', b'model="synthetic"\n', 0o600, None)
        self.assertEqual(host_security(admin), admin_security)
        config = admin / 'config.toml'
        file_security = self.host_acl(config, owner='BA')
        identity = config.stat().st_ino
        host_atomic(config, b'model="updated"\n', 0o600, config.read_bytes())
        self.assertEqual(host_security(config), file_security)
        self.assertEqual(config.stat().st_ino, identity)

    def test_existing_host_security_preflight_rollback_and_conflicts(self):
        from codex_connect import host_atomic, host_stage
        native = self.home / 'host-codex'; native.mkdir()
        self.host_acl(native)
        path = native / 'config.toml'; path.write_bytes(b'original')
        original = self.host_acl(path, inherited=True)
        with host_stage(path, b'installed', 0o600, b'original') as commit: commit()
        self.assertEqual(host_security(path), original)
        host_atomic(path, b'original', 0o600, b'installed')
        self.assertEqual(host_security(path), original)
        with host_stage(path, b'candidate', 0o600, b'original'):
            with self.assertRaises(OSError): path.write_bytes(b'external')
        path.write_bytes(b'external')
        with self.assertRaises(ValueError): host_atomic(path, b'candidate', 0o600, b'original')
        self.assertEqual(path.read_bytes(), b'external')
        with host_stage(path, b'candidate', 0o600, b'external') as commit:
            changed = self.host_acl(path)
            commit()
        self.assertEqual(host_security(path), changed)
        self.assertEqual(list(native.glob('.client-host-*')), [])

    def test_existing_host_handle_excludes_writes_and_renames_but_allows_readers(self):
        from codex_connect import host_stage
        native = self.home / 'host-codex'; native.mkdir()
        self.host_acl(native)
        config = native / 'config.toml'; config.write_bytes(b'original')
        identity = config.stat().st_ino
        with host_stage(config, b'updated', 0o600, b'original') as commit:
            self.assertEqual(config.read_bytes(), b'original')
            with self.assertRaises(OSError):
                with config.open('r+b'): pass
            other = native / 'outside'; other.write_bytes(b'outside')
            with self.assertRaises(OSError): os.replace(other, config)
            commit()
        self.assertEqual(config.read_bytes(), b'updated')
        self.assertEqual(config.stat().st_ino, identity)

    def test_readable_sacl_is_unchanged_without_production_security_api_calls(self):
        from codex_connect import host_atomic
        path = self.home / 'audited-config.toml'; path.write_bytes(b'original')
        try: original = host_security(path, 15)
        except ValueError: self.skipTest('Audit descriptor observation needs a pre-authorized test token')
        host_atomic(path, b'updated', 0o600, b'original')
        self.assertEqual(host_security(path, 15), original)

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
        fresh = getattr(self, 'fresh_tools', False)
        tools_only = getattr(self, 'tools_only', False)
        old_version = '0.5.6' if fresh else '0.5.5' if tools_only else '0.5.0'
        for version in sorted({old_version, '0.5.3', '0.5.6'}):
            payload = self.root / version
            release(ROOT, payload, version, data_mode='personal-content-https', target_platform=target)
            if version == '0.5.0': legacy_package(payload)
            payloads[version] = payload
        def updater(version):
            payload = payloads[version]
            manifest = json.loads((payload / 'releases/manifest.json').read_bytes())
            class Source:
                def latest(self):
                    return {'0.5.0': 'a', '0.5.3': 'b', '0.5.5': 'd', '0.5.6': 'c'}[version] * 40, manifest, lambda name: (payload / name).read_bytes()
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
            if not (tools_only or fresh):legacy_package(output)
            return result
        options = dict(access_mode=True, dependency_preparer=lambda *a, **k: materials, store=store, transport=peer,
            confirm=lambda *a: self.fail('No invitation prompt'), secure_input=lambda: 'SYNTHETIC_ENROLLMENT_MARKER_12345678901234567890')
        import pending_install_recovery as recovery
        real_prepare = wizard.prepare
        def old_prepare(*args, **kwargs):return recovery.legacy_plan(real_prepare(*args, **kwargs))
        if fresh:
            options['installer'] = lambda plan, digest: content_install.apply(plan, digest, lambda p: check_install_contract(self, p))
            first = wizard.run(self.home, native, connection, wizard.sha(wizard.canonical(connection)), updater=updater(old_version), **options)
            self.assertTrue(first['installed'], first)
            self.assertTrue(content_install.inspect(self.home)['installed'])
            self.assertEqual(len(content_install.private_json(self.home / '.config/agent-brain-client/first-install/install-plan.json')['mcp']['enabled_tools']), 9)
            return
        with patch.object(wizard, 'export', side_effect=old_export), patch.object(wizard, 'prepare', side_effect=old_prepare), patch.object(content_install, 'prepare', side_effect=old_prepare):
            first = wizard.run(self.home, native, connection, wizard.sha(wizard.canonical(connection)), updater=updater(old_version), **options)
        self.assertFalse(first['installed'], first)
        self.assertEqual(backend.record['phase'], 'active')
        identity = copy.deepcopy(backend.record)
        self.assertEqual(config.read_bytes(), initial_config)
        state = self.home / '.config/agent-brain-client/first-install'
        old_plan = (state / 'install-plan.json').read_bytes()
        self.assertEqual(json.loads(old_plan)['mcp']['enabled_tools'], ['content_catalog', 'content_put', 'content_read'])
        old_bootstrap = state / 'bootstrap'
        if not tools_only:self.assertEqual((old_bootstrap / 'scripts/brain_lock.py').read_bytes(), old_detector)
        options['secure_input'] = lambda: self.fail('Recovery must not request a new credential')
        checks = []
        def offline_bootstrap_probe(plan):
            check_install_contract(self, plan)
            checks.append(True)
            if tools_only and len(checks) == 2:
                raise OSError('Synthetic postcheck failure after native write')
        options['installer'] = lambda plan, digest: content_install.apply(plan, digest, offline_bootstrap_probe)
        original_save = recovery.save
        def interrupted(path, value):
            original_save(path, value)
            if Path(path).name == 'install-plan.json':
                raise OSError('SYNTHETIC_PLAN_PIN_INTERRUPTION')
        with patch.object(recovery, 'save', side_effect=interrupted):
            interrupted_result = wizard.run(self.home, native, connection, wizard.sha(wizard.canonical(connection)), updater=updater('0.5.6' if tools_only else '0.5.3'), **options)
        self.assertFalse(interrupted_result['installed'])
        self.assertEqual(config.read_bytes(), initial_config)
        self.assertEqual(backend.record, identity)
        # Normal startup may have advanced the independently verified runtime
        # after a prior failed install; retry must retain the fixed bootstrap,
        # profile, nonce and original plan recovery without requiring input.
        second = wizard.run(self.home, native, connection, wizard.sha(wizard.canonical(connection)),
            updater=updater('0.5.6'), **options)
        if tools_only:
            self.assertFalse(second['installed'], second)
            self.assertEqual(config.read_bytes(), initial_config)
            self.assertEqual(backend.record, identity)
            receipt = content_install.private_json(self.home / '.config/agent-brain-client/content-install-receipt.json')
            self.assertEqual(receipt['phase'], 'rolled-back')
            second = wizard.run(self.home, native, connection, wizard.sha(wizard.canonical(connection)),
                updater=lambda: self.fail('Exact rollback retry must not replace runtime'), **options)
        self.assertTrue(second['installed'], second)
        self.assertEqual(backend.record, identity)
        self.assertEqual(json.loads((state / 'releases/active.json').read_bytes())['version'], '0.5.6')
        journal = content_install.private_json(state / ('tools-recovery.json' if tools_only else 'platform-recovery.json'))
        self.assertEqual(journal['phase'], 'complete')
        self.assertEqual(journal['before'], json.loads(old_plan))
        if not tools_only:self.assertEqual((old_bootstrap / 'scripts/brain_lock.py').read_bytes(), old_detector)
        self.assertTrue(content_install.inspect(self.home)['installed'])

    def test_enrolled_055_three_tool_plan_recovers_without_identity_or_scope_change(self):
        self.tools_only = True
        self.test_enrolled_pre_native_050_recovers_to_verified_release_without_new_input()

    def test_fresh_normal_entry_compares_real_runtime_tools_before_and_after_install(self):
        self.fresh_tools = True
        self.test_enrolled_pre_native_050_recovers_to_verified_release_without_new_input()

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
        native = self.home / 'codex'; native.mkdir()
        parent_security = self.host_acl(native)
        config = native / 'config.toml'; config.write_text('model="synthetic"\n', encoding='utf-8')
        file_security = host_security(config)
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
            calls = []
            before_config = config.read_bytes()
            before_identity = config.stat().st_ino
            def fail_postcheck(value):
                calls.append(True)
                if len(calls) == 2: raise OSError('Synthetic failed postcheck')
            failed = content_install.apply(plan, pin, fail_postcheck)
            self.assertEqual(failed['status'], 'content-install-rolled-back')
            self.assertEqual(config.read_bytes(), before_config)
            self.assertEqual(config.stat().st_ino, before_identity)
            self.assertEqual(host_security(config), file_security)
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
            self.assertEqual(host_security(native), parent_security)
            self.assertEqual(host_security(config), file_security)
            args = SimpleNamespace(device_home=str(self.home), operator=None, operator_sha256=None, profile_id='f'*32)
            hooks.install(args)
            self.assertEqual(host_security(native),parent_security)
            self.assertEqual(host_security(config),file_security)
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

    def test_uninstall_new_private_lease_keeps_existing_host_security_separate(self):
        from client_lifecycle import lease
        directory=private_directory(self.home/'private-lease-state')
        path=directory/'new-runtime.lock'
        with lease(path):
            private_mode(path,owner=True)
        identity=path.stat().st_ino
        with lease(path):pass
        self.assertEqual(path.stat().st_ino,identity)
        foreign=directory/'existing-host.lock';foreign.write_bytes(b'')
        security=self.host_acl(foreign)
        with self.assertRaises(ValueError):lease(foreign)
        self.assertEqual(host_security(foreign),security)

    def test_stale_053_launcher_057_active_handover_and_retained_reinstall(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from build_client import release,export
        import mac_first_install as wizard
        import content_install as ci
        import client_lifecycle as lifecycle
        import global_memory_install as hooks
        import global_memory as gm
        from client_release import ClientReleases
        from content_cache import ContentAdapter
        from test_content_outbox import Cloud,RULE,SKILL
        from test_https_content import Backend
        from contextlib import nullcontext
        native=self.home/'codex';native.mkdir();config=native/'config.toml';config.write_bytes(b'model="synthetic"\n')
        directory_security=self.host_acl(native);file_security=host_security(config);inode=config.stat().st_ino
        target=native_platform()
        connection={'schema':1,'endpoint':'https://synthetic.invalid','grant':{'adapter':'codex','platform':target,'actor':'synthetic-owner','scopes':['read','put'],'artifact_types':['memory','rule','skill'],'ids':{k:'personal-owner' for k in ('memory','rule','skill')}},
                    'release':{'origin':wizard.OFFICIAL_ORIGIN,'ref':'refs/heads/releases/personal-https-windows-stable','anchor_commit':'a'*40}}
        payloads={}
        for version in ('0.5.7','0.5.8'):
            payloads[version]=self.root/version
            release(ROOT,payloads[version],version,data_mode='personal-content-https',target_platform=target)
        selected=['0.5.7']
        def channel(path,pin,**kwargs):
            value=ClientReleases(path,pin,**kwargs)
            class Source:
                def latest(self):
                    payload=payloads[selected[0]];manifest=json.loads((payload/'releases/manifest.json').read_bytes())
                    return ('a' if selected[0]=='0.5.7' else 'b')*40,manifest,lambda name:(payload/name).read_bytes()
            value.engine.source=Source()
            return value
        def update():
            policy=self.home/'.config/agent-brain-client/first-install/release-policy.json'
            return channel(policy,wizard.sha(policy.read_bytes())).update()
        def old_export(source,output,**options):
            result=export(source,output,**options)
            fixture=ROOT/'tests/fixtures/windows_launcher_053'
            pins=json.loads((fixture/'manifest.json').read_bytes())['files']
            for name,pin in pins.items():
                raw=(fixture/name).read_bytes();self.assertEqual(wizard.sha(raw),pin)
                destination='adapters/codex/'+name.removesuffix('.fixture')
                (Path(output)/destination).write_bytes(raw);result['files'][destination]=pin
            (Path(output)/'export-manifest.json').write_text(json.dumps(result),encoding='utf-8')
            return result
        backend=Backend();store=SimpleNamespace(load=backend.load,save=backend.save,lease=nullcontext)
        grant=connection['grant']
        peer=SimpleNamespace(prepare=lambda code,nonce,profile:{'phase':'pending','nonce':nonce,'credential':'t'*40,'device_id':'d'*32,'grant':grant},confirm=lambda token,nonce:{'phase':'active','device_id':'d'*32,'grant':grant})
        options=dict(access_mode=True,dependency_preparer=lambda *a,**k:wizard.prepare_dependencies(self.home),store=store,transport=peer,confirm=lambda *a:'Continue',secure_input=lambda:'SYNTHETIC_ENROLLMENT_MARKER_12345678901234567890',installer=lambda p,d:ci.apply(p,d,lambda value:None))
        with patch.object(wizard,'export',side_effect=old_export):
            first=wizard.run(self.home,native,connection,wizard.sha(wizard.canonical(connection)),updater=update,**options)
        self.assertTrue(first['installed'],first);identity=copy.deepcopy(backend.record)
        state=self.home/'.config/agent-brain-client';meta=ci.private_json(state/'codex-mcp.json')
        table=ci.tomllib.loads(config.read_text(encoding='utf-8'))['mcp_servers'][ci.NAME]
        self.assertEqual(len(table['enabled_tools']),9)
        # Execute the historical validator in its own process. It reconstructs
        # three tools and rejects the authenticated nine-tool registration.
        code="import sys,json;sys.path.insert(0,sys.argv[1]);import content_install;print(json.dumps(content_install.inspect(sys.argv[2])))"
        observed=subprocess.run([table['command'],'-I','-B','-c',code,str(Path(meta['package'])/'adapters/codex'),str(self.home)],capture_output=True,text=True,timeout=60)
        self.assertEqual(observed.returncode,0,observed.stderr)
        self.assertEqual(json.loads(observed.stdout)['reason'],'mcp-binding-changed')
        selected[0]='0.5.8'
        fixed=wizard.manage_update(self.home,'update',channel_factory=channel)
        self.assertEqual(fixed['status'],'client-update-updated',fixed)
        self.assertTrue(fixed['native_changed']);self.assertTrue(fixed['reload_required'])
        self.assertEqual(fixed['active_package_version'],'0.5.8');self.assertTrue(ci.inspect(self.home)['installed'])
        meta=ci.private_json(state/'codex-mcp.json');policy=ci.private_json(meta['release_policy']);runtime=policy['runtime']
        # A failed old launcher can leave MCP registered with no content cache.
        # Ordinary retained-data uninstall/reinstall must work from that state.
        self.assertFalse(Path(runtime['content_cache']).exists())
        config.write_bytes(config.read_bytes().replace(b'enabled = true',b'enabled = false'))
        self.assertEqual(lifecycle.uninstall(self.home,lambda message:'Continue')['status'],'client-uninstalled')
        private_mode(Path(runtime['content_cache']),directory=True,owner=True)
        options['secure_input']=lambda:self.fail('No new credential input for MCP-only reinstall')
        restored=wizard.run(self.home,native,connection,wizard.sha(wizard.canonical(connection)),updater=update,**options)
        self.assertTrue(restored['installed'],restored);self.assertEqual(backend.record,identity)
        cloud=Cloud();content=ci.private_json(runtime['content_policy']);cloud.grant=copy.deepcopy(grant)
        cloud.records={('rule','personal-owner'):dict(RULE,enabled=True),('skill','personal-owner'):dict(SKILL,enabled=True,files={'SKILL.md':SKILL['files']['SKILL.md'].replace('synthetic-skill','personal-owner')})}
        adapter=ContentAdapter(self.home,runtime['content_cache'],content,cloud,codex_home=native)
        self.assertEqual(adapter.refresh()['status'],'content-refreshed')
        args=SimpleNamespace(device_home=str(self.home),profile_id=None,operator=None,operator_sha256=None)
        hooks.install(args);self.assertFalse(gm.approved(gm.load(self.home)))
        self.assertEqual(host_security(config),file_security);self.assertEqual(host_security(native),directory_security);self.assertEqual(config.stat().st_ino,inode)
        # Pause only this MCP before owned removal. Identity, source settings,
        # snapshots and the original file object survive normal uninstall.
        config.write_bytes(config.read_bytes().replace(b'enabled = true',b'enabled = false'))
        removed=lifecycle.uninstall(self.home,lambda message:'Continue')
        self.assertEqual(removed['status'],'client-uninstalled',removed)
        self.assertNotIn(hooks.BEGIN,config.read_text(encoding='utf-8'))
        self.assertEqual(backend.record,identity);self.assertTrue(gm.settings_path(self.home).exists())
        options['secure_input']=lambda:self.fail('No new credential input on reinstall')
        again=wizard.run(self.home,native,connection,wizard.sha(wizard.canonical(connection)),updater=update,**options)
        self.assertTrue(again['installed'],again)
        self.assertEqual(adapter.refresh()['status'],'content-refreshed')
        hooks.install(args)
        self.assertTrue(hooks.installation_status(gm.load(self.home)))
        self.assertEqual(backend.record,identity);self.assertEqual(host_security(config),file_security);self.assertEqual(config.stat().st_ino,inode)

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
