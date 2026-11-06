from agent_brain_client.adapters.platforms.tree import is_link
from agent_brain_client.adapters.platforms.posix.tree import PosixTree
from agent_brain_client.adapters.platforms.windows.tree import WindowsTree
import agent_brain_client.adapters.storage.private_files as adapters_storage_private_files
import agent_brain_client.adapters.storage.install_state as adapters_storage_install_state
import agent_brain_client.interfaces.cli.install as interfaces_cli_install
import agent_brain_client.runtime.dependencies as runtime_dependencies
import agent_brain_client.adapters.harnesses.codex.memory_inventory as adapters_harnesses_codex_memory_inventory
import agent_brain_client.adapters.storage.collection as source_policy
import agent_brain_client.adapters.storage.metadata as metadata
"""Real Windows API/transaction tests with only synthetic private state."""
import copy
import ctypes
import hashlib
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
sys.path[:0] = [str(ROOT / n) for n in ('src', 'src', 'src', 'tests')]
from agent_brain_client.adapters.platforms.filesystem import private_directory, private_mode, locked, LockTimeout, command_line, native_platform, cache_mode




def host_security(path, information=7):
    """Test-only diagnostic; production writes never need descriptor access."""
    from agent_brain_client.adapters.platforms.windows.native import libraries
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

    The child executes the built runtime's Server, CodexContentSession and read-only
    protocol check. Its tool list is never copied from the expected plan.
    """
    from unittest.mock import patch
    import agent_brain_client.adapters.harnesses.codex.installation as content_install
    real_run = subprocess.run
    environment = dict(plan['mcp']['env'], HOME=plan['device_home'])
    run = real_run([plan['mcp']['command'], *plan['mcp']['args'], '--bootstrap-check'],
                   env=environment, capture_output=True, timeout=30)
    test.assertEqual(run.returncode, 0, run.stderr)
    state = Path(plan['device_home']) / '.config/agent-brain-client/first-install'
    active = json.loads((state / 'releases/active.json').read_bytes())
    def protocol_command(argv, **options):
        test.assertIn('--installation-check', argv)
        runtime = adapters_storage_private_files.private_json(plan['release_policy'])['runtime']
        return real_run([plan['mcp']['command'], '-I', '-B',
            str(ROOT / 'tests/fixtures/checkpoint_probe.py'), active['package'], '--installation-result',
            '--content-policy', runtime['content_policy'], '--content-policy-sha256', runtime['content_policy_sha256'],
            '--https-profile', runtime['https_profile'], '--https-profile-sha256', runtime['https_profile_sha256']], **options)
    with patch.object(content_install.subprocess, 'run', side_effect=protocol_command):
        content_install.probe(plan)


@unittest.skipUnless(os.name == 'nt', 'Native Windows APIs required')
class WindowsNative(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        from agent_brain_client.adapters.platforms.windows.native import protect_new
        protect_new(self.root, directory=True)
        self.home = private_directory(self.root / 'User Space & 文本')

    def host_acl(self, path, *, owner=None, inherited=False):
        from agent_brain_client.adapters.platforms.windows.native import libraries, user_sid
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
        from agent_brain_client.adapters.storage.host_files import host_atomic
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

    def test_global_capture_accepts_existing_cache_acls_and_replays_original_queue(self):
        from unittest.mock import patch
        import agent_brain_client.runtime.collection as gm
        from agent_brain_client.domain.scopes import digest
        from agent_brain_client.adapters.storage.host_files import atomic
        from agent_brain_client.adapters.harnesses.codex.content import CodexContentSession
        from test_content_outbox import Cloud, POLICY
        from test_event_hook import accepted
        home = self.home
        project = home / 'project'; project.mkdir()
        local = {'kind':'project', 'cwd':str(project), 'project_root':str(project), 'repository_root':str(project)}
        scope = {'schema':1, 'profile_id':'a'*32, 'all_projects':True, 'include_roots':[],
                 'exclude_roots':[], 'non_project_roots':[], 'recall_personal':True}
        value = {'device_home':str(home), 'enabled':True, 'scope':scope,
                 'approved_scope_sha256':digest(scope), 'server_scope_sha256':digest(scope)}
        settings = source_policy.settings_path(home).parent
        private_directory(settings)
        profile = settings / 'profile.json'; policy = settings / 'content.json'
        atomic(profile, json.dumps({'grant':{'actor':'synthetic-owner'}}).encode(), 0o600)
        atomic(policy, json.dumps(POLICY).encode(), 0o600)
        r = {'https_profile':str(profile), 'https_profile_sha256':metadata.digest(profile.read_bytes()),
             'content_policy':str(policy), 'content_policy_sha256':metadata.digest(policy.read_bytes()),
             'codex_home':str(home / '.codex')}
        cloud = Cloud(); cloud.profile_sha = r['https_profile_sha256']
        cloud.status = lambda session: {'source_admitted':True}
        cache = settings / 'outboxes' / digest(local)
        # Match the old global adapter's ordinary mkdir, then add a legitimate
        # inherited host principal. Neither update nor capture may rewrite it.
        cache.mkdir(parents=True)
        original_security = self.host_acl(cache)
        retained = cache / 'retained-evidence'; retained.write_bytes(b'synthetic retained data')
        payload = {'cwd':str(project), 'session_id':'synthetic-session', 'turn_id':'synthetic-turn',
                   'hook_event_name':'UserPromptSubmit', 'prompt':'Synthetic capture ACL regression'}
        stop = dict(payload, hook_event_name='Stop', stop_hook_active=False,
                    last_assistant_message='Synthetic final answer')
        sent = []
        def offline(request):
            sent.append(copy.deepcopy(request))
            return {'request_id':request['request_id'], 'error':{'code':'transport_unconfirmed','retryable':True},
                    'persistence':{'durable':False}}
        cloud.request = offline
        with patch('agent_brain_client.adapters.storage.registration.registration', return_value=({}, {'runtime':r})), patch.object(gm, 'CollectionClient', return_value=cloud):
            self.assertEqual(gm.submit_hook(value, local, payload)['status'], 'staged')
            sandbox = 'S-1-5-21-111111111-222222222-333333333-1002'
            for item in (cache / 'hook-turns', cache / 'event-hook.lock', next((cache / 'hook-turns').glob('*.json'))):
                self.assertIn(sandbox, host_security(item))
            pending = gm.submit_hook(value, local, stop)
            self.assertEqual(pending['local_queue']['state'], 'pending')
            request = sent[0]
            # Simulate existing data/locks from a prior run acquiring the same
            # host ACE. Strict credential checks must still reject that ACL.
            security = {}
            for item in cache.rglob('*'):
                security[item] = self.host_acl(item)
            with self.assertRaises(ValueError): private_mode(cache, directory=True)
            restarted = CodexContentSession(home, cache, POLICY, cloud, codex_home=r['codex_home'])
            self.assertEqual(restarted._queued()[0]['request'], request)
            from agent_brain_client.adapters.harnesses.codex.lifecycle import queue_counts
            self.assertEqual(queue_counts(restarted)['pending'], 1)
            cloud.request = lambda req: (sent.append(copy.deepcopy(req)), accepted(req))[1]
            self.assertEqual(gm.submit_hook(value, local, stop)['local_queue']['state'], 'confirmed')
            self.assertEqual(sent, [request, request])
            self.assertEqual(len(list((cache / 'outbox').glob('*.json'))), 1)
            # The existing lock and directory objects retain their ACLs.
            for item, before in security.items():
                if item.is_dir() or item.suffix == '.lock': self.assertEqual(host_security(item), before)
        self.assertEqual(host_security(cache), original_security)
        self.assertEqual(retained.read_bytes(), b'synthetic retained data')

    def test_administrator_owned_host_directory_and_file_keep_security(self):
        from agent_brain_client.adapters.storage.host_files import host_atomic
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
        from agent_brain_client.adapters.storage.host_files import host_atomic,host_stage
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
        from agent_brain_client.adapters.storage.host_files import host_stage
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
        from agent_brain_client.adapters.storage.host_files import host_atomic
        path = self.home / 'audited-config.toml'; path.write_bytes(b'original')
        try: original = host_security(path, 15)
        except ValueError: self.skipTest('Audit descriptor observation needs a pre-authorized test token')
        host_atomic(path, b'updated', 0o600, b'original')
        self.assertEqual(host_security(path, 15), original)

    def test_native_platform_in_minimal_environment_and_forged_processor_variables(self):
        code = "import sys;sys.path.insert(0,sys.argv[1]);from agent_brain_client.adapters.platforms.filesystem import require_platform;print(require_platform('minimal-environment'))"
        for extra in ({}, {'PROCESSOR_ARCHITECTURE': 'x86', 'PROCESSOR_ARCHITEW6432': 'unsupported'}):
            environment = dict(extra, SystemRoot=os.environ['SystemRoot'])
            run = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(ROOT / 'src')],
                env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout.strip(), native_platform())

    def test_python_selector_skips_store_alias_and_broken_candidate(self):
        selector = ROOT / 'bootstrap/windows/python.ps1'
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





    def test_credentials_native_roundtrip_cas_size_and_removal(self):
        from agent_brain_client.adapters.platforms.windows.native import WindowsCredentials
        from agent_brain_client.adapters.platforms.credentials import CredentialStore
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
        from agent_brain_client.adapters.platforms.windows.native import libraries, user_sid
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
        code = "import sys;sys.path.insert(0,sys.argv[1]);from agent_brain_client.adapters.platforms.filesystem import locked\nwith locked(sys.argv[2],shared=True):\n print('ready',flush=True);sys.stdin.readline()"
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', code, str(ROOT / 'src'), str(file)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            with locked(file, timeout=0, shared=True): pass
            with self.assertRaises(LockTimeout):
                with locked(file, timeout=0): pass
        finally:
            child.stdin.close(); child.wait(timeout=10); child.stdout.close()
        with locked(file, timeout=1): pass

    def test_same_id_concurrent_retry_returns_both_callers_and_uploads_once(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from agent_brain_client.adapters.harnesses.codex.content import CodexContentSession
        from test_content_outbox import Cloud, POLICY, MEMORY
        for index in range(10):
            with self.subTest(iteration=index):
                cache = self.home / ('concurrent-cache-' + str(index))
                cloud = Cloud()
                def adapter(): return CodexContentSession(self.home, cache, POLICY, cloud, codex_home=self.home / '.codex')
                adapter()  # Exercise fresh lock creation, as in the reported test.
                request = {'request_id': 'synthetic_concurrent_retry_' + str(index), 'method': 'content_put',
                    'params': {'kind': 'memory', 'id': 'synthetic-topic', 'record': MEMORY, 'expected_revision': 'missing'}}
                barrier = threading.Barrier(2)
                def retry():
                    client = adapter(); barrier.wait(timeout=10)
                    return client.invoke(copy.deepcopy(request))
                with ThreadPoolExecutor(max_workers=2) as workers:
                    futures = [workers.submit(retry) for _ in range(2)]
                    results = [future.result(timeout=60) for future in futures]
                self.assertEqual([row['local_queue']['state'] for row in results], ['confirmed', 'confirmed'])
                self.assertEqual(cloud.puts, 1)
                self.assertEqual(adapter().queue_status()['confirmed'], 1)

    def test_hook_command_preserves_spaces_shell_symbols_unicode_and_quotes(self):
        expected = 'literal %PATH% $env:PATH & 文本 "quote"'
        command = command_line([sys.executable, '-I', '-B', '-c', 'print(' + repr(expected) + ')'])
        self.assertLess(len(command), 8191)
        run = subprocess.run(command, shell=True, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), expected)

    def test_real_shared_installer_and_owned_global_hook_transaction(self):
        import agent_brain_client.runtime.installation as installer
        import agent_brain_client.adapters.harnesses.codex.installation as content_install
        import agent_brain_client.adapters.harnesses.codex.collection_installation as hooks
        import agent_brain_client.runtime.collection as global_memory
        from types import SimpleNamespace
        native = self.home / 'codex'; native.mkdir()
        parent_security = self.host_acl(native)
        config = native / 'config.toml'; config.write_text('model="synthetic"\n', encoding='utf-8')
        file_security = host_security(config)
        connection = {'schema': 1, 'endpoint': 'https://synthetic.invalid',
            'grant': {'adapter': 'codex', 'platform': native_platform(), 'actor': 'synthetic-owner', 'scopes': ['read','put'],
                      'artifact_types': ['memory','rule','skill'], 'ids': {k:'personal-owner' for k in ('memory','rule','skill')}},
            'release': {'origin': installer.OFFICIAL_ORIGIN, 'ref': 'refs/heads/releases/personal-https-windows-stable', 'anchor_commit': 'a'*40}}
        materials = runtime_dependencies.prepare_dependencies(self.home)
        from unittest.mock import patch
        from dev.build.package import release
        from agent_brain_client.adapters.packages.client import ClientReleases
        payload = self.root / 'synthetic-release'
        release(ROOT, payload, '0.5.0', data_mode='personal-content-https', target_platform=native_platform())
        manifest = json.loads((payload / 'releases/manifest.json').read_bytes())
        class Source:
            def latest(self):
                return 'a'*40, manifest, lambda name: (payload / name).read_bytes()
        def update():
            state = self.home / '.config/agent-brain-client/first-install'
            policy = state / 'release-policy.json'
            client = ClientReleases(policy, adapters_storage_install_state.sha(policy.read_bytes()))
            client.engine.source = Source()
            result = client.engine.check()
            result['active'] = client.verified_active()
            return result
        with patch.dict(os.environ, CODEX_HOME=str(native)):
            plan, pin, _ = installer.prepare_materials(materials, self.home, native, connection,
                adapters_storage_install_state.sha(installer.canonical(connection)), updater=update)
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
            receipt = adapters_storage_private_files.private_json(state / 'content-install-receipt.json')
            receipt['phase'] = 'applying'
            adapters_storage_private_files.save(state / 'content-install-receipt.json', receipt)
            adapters_storage_install_state.same_private(state / 'access-bootstrap/endpoint.json', {'schema': 1, 'endpoint': connection['endpoint']})
            adapters_storage_install_state.same_private(state / 'access-bootstrap/connection.json', connection)
            original_run = installer.run
            def resume(home, codex, value, digest, **options):
                return original_run(home, codex, value, digest, **options,
                    confirm=lambda *a: self.fail('No invitation or consent prompt during exact recovery'),
                    dependency_preparer=lambda *a, **k: materials,
                    secure_input=lambda: self.fail('No new credential input'),
                    updater=lambda: self.fail('No replacement release'),
                    installer=lambda p, d: content_install.apply(p, d, lambda value: None))
            import io
            with patch.object(Path, 'home', return_value=self.home), patch.object(sys, 'argv', ['agent_brain_client.runtime.installation.py']), patch.object(installer, 'run', side_effect=resume), patch.object(sys, 'stdout', io.StringIO()) as output:
                self.assertEqual(interfaces_cli_install.main(), 0, output.getvalue())
            self.assertTrue(json.loads(output.getvalue())['installed'])
            self.assertTrue(content_install.inspect(self.home).get('registration_verified'))
            again = content_install.apply(plan, pin, lambda value: None)
            self.assertTrue(again['installed'])
            self.assertEqual(host_security(native), parent_security)
            self.assertEqual(host_security(config), file_security)
            args = SimpleNamespace(device_home=str(self.home), operator=None, operator_sha256=None, profile_id='f'*32)
            hooks.install(args)
            from test_windows_install_closure import torn_hook
            before_hook=torn_hook(self,self.home,native)
            self.assertFalse(content_install.inspect(self.home).get('registration_verified'))
            with patch.object(Path,'home',return_value=self.home),patch.object(sys,'argv',['agent_brain_client.runtime.installation.py']),patch.object(installer,'run',side_effect=lambda *a,**k:content_install.inspect(self.home)),patch.object(installer,'finish_install',side_effect=lambda home,value:value),patch.object(sys,'stdout',io.StringIO()) as output:
                self.assertEqual(interfaces_cli_install.main(),0,output.getvalue())
            self.assertEqual(config.read_bytes(),before_hook)
            hooks.install(args)
            self.assertTrue(hooks.installation_status(source_policy.load(self.home)))
            self.assertEqual(host_security(native),parent_security)
            self.assertEqual(host_security(config),file_security)
            value = source_policy.load(self.home)
            text = config.read_text(encoding='utf-8')
            self.assertIn('commandWindows', text)
            self.assertEqual(value['legacy_projects'], [])
            self.assertFalse(source_policy.approved(value))
            checkout = self.home / 'Public 工具 checkout'
            for name in ('src', 'bootstrap', 'bin'):
                shutil.copytree(ROOT / name, checkout / name)
            environment = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home), CODEX_HOME=str(native))
            for shell in ('powershell.exe', 'pwsh.exe'):
                if shutil.which(shell) is None:
                    continue
                for script, arguments, field in (
                    ('bootstrap/windows/install.ps1', ['-Action', 'status'], 'installed'),
                    ('bin/memory.ps1', ['global-status', '--device-home', str(self.home)], 'global_entry_installed'),
                ):
                    with self.subTest(shell=shell, script=script):
                        command = [shell, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(checkout / script), *arguments]
                        run = subprocess.run(command, env=environment, capture_output=True, text=True, encoding='utf-8', timeout=60)
                        self.assertEqual(run.returncode, 0, run.stderr)
                        self.assertTrue(json.loads(run.stdout)[field], run.stdout)
            hooks.uninstall(args)
            self.assertNotIn(hooks.BEGIN, config.read_text(encoding='utf-8'))
            self.assertTrue(content_install.inspect(self.home)['registration_verified'])

    def test_uninstall_new_private_lease_keeps_existing_host_security_separate(self):
        from agent_brain_client.adapters.harnesses.codex.lifecycle import lease
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


    def test_offline_outbox_reconnect_and_rule_skill_mapping_share_same_core(self):
        from test_content_outbox import Cloud, POLICY, MEMORY, RULE, SKILL
        from agent_brain_client.adapters.harnesses.codex.content import CodexContentSession
        cloud = Cloud(); cloud.grant.update(platform=native_platform())
        cache = self.home / 'cache'
        def adapter(): return CodexContentSession(self.home, cache, copy.deepcopy(POLICY), cloud, codex_home=self.home / 'codex')
        first = adapter(); cloud.offline = True
        request = {'request_id':'synthetic_windows_write_001','method':'content_put',
                   'params':{'kind':'memory','id':'synthetic-topic','record':MEMORY,'expected_revision':'missing'}}
        self.assertTrue(first.invoke(request)['local_queue']['saved'])
        cloud.offline = False
        cloud.records.update({('rule','synthetic-rule'):RULE, ('skill','synthetic-skill'):SKILL})
        self.assertEqual(adapter().refresh()['status'], 'content-refreshed')
        self.assertEqual(cloud.puts, 1)
        self.assertEqual(adapter().queue_status()['confirmed'], 1)
        cache_mode(cache / 'active.json', owner=True)
        self.assertEqual([r for r in cloud.calls if r['method']=='content_put'], [request, request])

    def test_refresh_skips_unchanged_agents_held_by_read_only_host(self):
        from ctypes import wintypes as W
        from agent_brain_client.adapters.platforms.windows.native import libraries
        from agent_brain_client.adapters.harnesses.codex.content import CodexContentSession
        from test_content_outbox import Cloud, POLICY, MEMORY
        cloud=Cloud();cloud.grant.update(platform=native_platform())
        adapter=CodexContentSession(self.home,self.home/'cache',copy.deepcopy(POLICY),cloud,codex_home=self.home/'codex')
        self.assertEqual(adapter.refresh()['status'],'content-refreshed')
        agents=self.home/'codex/AGENTS.md';before=agents.read_bytes()
        _,kernel=libraries()
        kernel.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,ctypes.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
        kernel.CreateFileW.restype=W.HANDLE
        kernel.CloseHandle.argtypes=[W.HANDLE]
        handle=kernel.CreateFileW(str(agents),0x80000000,1,None,3,0,None)
        self.assertNotEqual(handle,W.HANDLE(-1).value)
        try:
            request={'request_id':'synthetic_windows_busy_agents_001','method':'content_put',
                     'params':{'kind':'memory','id':'synthetic-topic','record':MEMORY,'expected_revision':'missing'}}
            result=adapter.invoke(request)
            self.assertTrue(result['local_queue']['server_saved'])
            self.assertEqual(result['local_refresh']['status'],'content-refreshed',result)
            self.assertEqual(agents.read_bytes(),before)
            self.assertEqual((self.home/'.config/agent-brain-client/content/memory/synthetic-topic.md').read_bytes(),MEMORY['body'].encode())
        finally:kernel.CloseHandle(handle)

# This module is already selected by Windows CI. Inherit the complete synthetic
# inventory/review/stage/CAS/outbox suite so native readers cannot be omitted.
import test_codex_memory_inventory as memory_inventory


@unittest.skipUnless(os.name == 'nt', 'Native Windows memory reader required')
class WindowsCodexMemoryImport(memory_inventory.CodexMemoryInventory):
    def junction(self,link,target):
        result=subprocess.run(['cmd','/d','/c','mklink','/J',str(link),str(target)],capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.addCleanup(lambda:link.rmdir() if link.exists() else None)

    def test_junction_roots_ancestors_and_children_never_follow_target(self):
        from agent_brain_client.adapters.harnesses.codex.memory_inventory import inventory_codex_memories
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);outside=home/'outside';outside.mkdir()
            (outside/'never-read.md').write_bytes(b'synthetic outside memory')
            root=home/'codex';root.mkdir()
            link=root/'memories';self.junction(link,outside)
            try:self.assertEqual(inventory_codex_memories(root)['status'],'unavailable')
            finally:link.rmdir()
            link.mkdir();child=link/'linked';self.junction(child,outside)
            try:
                result=inventory_codex_memories(root)
                self.assertEqual(result['counts'],{'excluded':1})
                self.assertIsNone(result['sources'][0]['source_fingerprint'])
            finally:child.rmdir()
            physical=home/'physical';(physical/'memories').mkdir(parents=True)
            (physical/'memories'/'never-read.md').write_bytes(b'synthetic junction ancestor')
            ancestor=home/'alias';self.junction(ancestor,physical)
            try:self.assertEqual(inventory_codex_memories(ancestor)['status'],'unavailable')
            finally:ancestor.rmdir()

    def test_configured_codex_home_junction_cannot_be_hidden_by_binding_resolution(self):
        from agent_brain_client.adapters.harnesses.codex.content import CodexContentSession
        from agent_brain_client.domain.errors import CacheBlocked
        from agent_brain_client.adapters.harnesses.codex.memory_inventory import inventory_codex_memories
        helper=memory_inventory.base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        physical=helper.home/'physical';root=physical/'memories';root.mkdir(parents=True)
        (root/'one.md').write_bytes(b'synthetic physical source')
        alias=helper.home/'configured-codex';self.junction(alias,physical)
        adapter=CodexContentSession(helper.home,helper.cache,memory_inventory.base.POLICY,helper.cloud,codex_home=alias)
        self.assertEqual(adapter.native_home,physical.resolve())
        self.assertEqual(adapter.binding['codex_home'],str(physical.resolve()))
        self.assertEqual(adapter.sources.inventory_memory_sources()['status'],'unavailable')
        row=inventory_codex_memories(physical)['sources'][0]
        # Even an existing ledger bound to the same canonical path must not
        # bypass the original configured-path reparse check on body reads.
        key=memory_inventory.contract.encode([row['source_id'],row['source_fingerprint']])
        from agent_brain_client.adapters.harnesses.codex.content import sha
        adapter._save('memory-inventory.json',{'schema':1,'binding':adapter.binding,'cursor':'synthetic',
            'status':'complete','sources':{sha(key):row}})
        with self.assertRaises(CacheBlocked):adapter.sources.read_memory_source(row['source_id'],row['source_fingerprint'])
        self.assertEqual(helper.cloud.calls,[])

    def test_directory_swap_attempt_fails_closed_and_later_junction_read_is_blocked(self):
        from unittest.mock import patch
        import agent_brain_client.adapters.harnesses.codex.content as cache
        helper=memory_inventory.base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        root=helper.home/'.codex/memories';group=root/'group';group.mkdir(parents=True)
        (group/'one.md').write_bytes(b'synthetic approved tree')
        row=helper.adapter.sources.inventory_memory_sources()['sources'][0]
        outside=helper.home/'outside';outside.mkdir();(outside/'one.md').write_bytes(b'synthetic outside tree')
        original=WindowsTree.open_directory
        def replace(tree,directory,entry,info):
            group.rename(root/'retained')
            self.junction(group,outside)
            return original(tree,directory,entry,info)
        try:
            with patch.object(WindowsTree,'open_directory',replace):
                result=adapters_harnesses_codex_memory_inventory.inventory_codex_memories(helper.adapter.native_home)
            changed=next(item for item in result['sources'] if item['status']=='unavailable')
            self.assertIsNone(changed['source_fingerprint'])
            # Windows may reject the rename itself while the parent is pinned.
            # Once the scan releases its handles, also exercise an actual
            # junction substitution between inventory and explicit body read.
            if not is_link(group.lstat()):
                group.rename(root/'retained')
                self.junction(group,outside)
            self.assertTrue(is_link(group.lstat()))
            with self.assertRaises(cache.CacheBlocked):helper.adapter.sources.read_memory_source(row['source_id'],row['source_fingerprint'])
        finally:
            if group.exists() and is_link(group.lstat()):group.rmdir()

    def test_pinned_directories_and_files_exclude_replacement_and_writers(self):
        import agent_brain_client.adapters.harnesses.codex.content as cache
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'memories';group=root/'group';group.mkdir(parents=True)
            target=group/'one.md';target.write_bytes(b'synthetic before')
            tree=WindowsTree(root)
            try:
                with tree.scan(root) as scan:entry=next(scan)
                directory=tree.open_directory(root,entry,tree.info(root,entry))
                tree.close_directory(root)  # Parent remains pinned after traversal.
                with self.assertRaises(OSError):root.rename(Path(tmp)/'moved-root')
                with self.assertRaises(OSError):group.rename(root/'moved-group')
                with tree.scan(directory) as scan:entry=next(scan)
                fd=tree.open_file(directory,entry,tree.info(directory,entry))
                with os.fdopen(fd,'rb') as source:
                    with self.assertRaises(OSError):target.write_bytes(b'competing writer')
                    replacement=Path(tmp)/'replacement';replacement.write_bytes(b'replacement')
                    with self.assertRaises(OSError):os.replace(replacement,target)
                    self.assertEqual(source.read(),b'synthetic before')
                target.write_bytes(b'after close')
                # A pre-existing writer causes a sharing violation, rather than
                # returning an unstable inventory fingerprint.
                with target.open('r+b'):
                    result=adapters_harnesses_codex_memory_inventory.inventory_codex_memories(Path(tmp))
                    self.assertEqual(result['counts'],{'unavailable':1})
            finally:tree.close()
            group.rename(root/'released-group')
            root.rename(Path(tmp)/'released-root')

    def test_unicode_codex_home_full_review_and_offline_restart_use_existing_outbox(self):
        from agent_brain_client.adapters.harnesses.codex.content import CodexContentSession
        from agent_brain_client.interfaces.mcp.content import ContentServer
        helper=memory_inventory.base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        native=helper.home/'中文 Codex 目录';root=native/'memories'/'中文目录';root.mkdir(parents=True)
        (root/'经历.md').write_bytes('合成事实：本周更正了指示牌。'.encode('utf-8'))
        def restart():return CodexContentSession(helper.home,helper.cache,memory_inventory.base.POLICY,helper.cloud,codex_home=native)
        adapter=restart();server=ContentServer(adapter)
        row=server.invoke('content_sources_inventory',{})['structuredContent']['sources'][0]
        params={'source_id':row['source_id'],'source_fingerprint':row['source_fingerprint']}
        body=server.invoke('content_sources_read',params)['structuredContent']
        self.assertEqual(body['text'],'合成事实：本周更正了指示牌。')
        self.assertFalse(body['data_transferred']);self.assertEqual(helper.cloud.calls,[])
        server.invoke('content_sources_review',dict(params,decision='shareable',reason_code='safe-cross-project-personal-summary'))
        record={'title':'Synthetic progress','description':'Synthetic Windows fixture','body':'Synthetic normalized experience','type':'feedback'}
        staged=server.invoke('content_sources_stage',dict(params,id='synthetic-topic',record=record))['structuredContent']
        self.assertFalse(staged['data_transferred']);self.assertEqual(helper.cloud.calls,[])
        helper.cloud.offline=True
        saved=server.invoke('content_put',{'kind':'memory','id':'synthetic-topic','record':record,'expected_revision':'missing',
            'request_id':'synthetic_windows_memory_001','reconciles':{'request_ids':[],'source_keys':[staged['source_key']]}})
        self.assertEqual(saved['structuredContent']['local_queue']['state'],'pending')
        self.assertFalse(saved['structuredContent']['persistence']['durable'])
        helper.cloud.offline=False;restored=restart();restored.refresh()
        self.assertEqual(helper.cloud.puts,1)
        self.assertEqual(helper.cloud.records[('memory','synthetic-topic')],record)
        self.assertEqual(restored.sources.import_status()['counts'],{'reconciled':1})
        self.assertEqual(restored.queue_status()['confirmed'],1)
        self.assertNotIn(body['text'],(helper.cache/'memory-inventory.json').read_text(encoding='utf-8'))


if __name__ == '__main__': unittest.main()
