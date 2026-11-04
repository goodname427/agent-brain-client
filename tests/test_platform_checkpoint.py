"""Exercise the actual MCP checkpoint under the launcher's minimal environment."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)] + [str(ROOT / n) for n in ('scripts', 'adapters/codex', 'tools')]
import connect
import remote_mcp
from brain_lock import native_platform
from build_client import release
from check_source import check_platform_boundary


class PlatformCheckpoint(unittest.TestCase):
    def test_root_legacy_discovery_uses_shared_platform_and_keeps_windows_gate(self):
        requests = []
        class Client:
            def __init__(self, *args):
                pass
            def request(self, request):
                requests.append(request)
                return {'result': {'core_api_version': 1}}
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            with patch.object(connect.sys, 'platform', 'linux'), patch.object(connect, 'native_platform', return_value='linux-x86_64') as detect, patch.object(connect, 'RemoteClient', Client):
                self.assertEqual(connect.connect(home, 'http://127.0.0.1:8765', home / 'synthetic.token')['status'], 'ready-to-register')
                detect.assert_called_once()
                self.assertEqual(requests[-1]['params']['platform'], 'linux-x86_64')
            with patch.object(connect.sys, 'platform', 'win32'), patch.object(connect, 'RemoteClient', side_effect=AssertionError('Windows legacy gate')):
                self.assertEqual(connect.connect(home, 'http://127.0.0.1:8765', home / 'synthetic.token')['status'], 'platform-not-supported')

    @unittest.skipUnless(os.name == 'nt' and sys.version_info[:3] <= (3, 11, 9), 'Historical Windows CPython 3.11 environment')
    def test_python311_blank_stdlib_architecture_still_reaches_real_checkpoint(self):
        code = """import json,platform,sys
from pathlib import Path
root=Path(sys.argv[1]);sys.path[:0]=[str(root/n) for n in ('scripts','adapters/codex')]
assert platform.machine()==''
from remote_mcp import Server
from brain_lock import require_platform
assert Server(None,'0.5.6').client_platform==require_platform('checkpoint-regression')
print(json.dumps({'stdlib_machine_empty':True,'fixed_checkpoint':Server(None,'0.5.6').client_platform}))
"""
        run = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(ROOT)],
            env={'SystemRoot': os.environ['SystemRoot']}, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(json.loads(run.stdout)['stdlib_machine_empty'])

    def test_default_checkpoint_uses_shared_detector_and_explicit_metadata_stays_validated(self):
        with patch.object(remote_mcp, 'native_platform', return_value='win32-arm64') as detect:
            self.assertEqual(remote_mcp.Server(None, '0.5.6').client_platform, 'win32-arm64')
            detect.assert_called_once()
        with patch.object(remote_mcp, 'native_platform', side_effect=AssertionError('Explicit metadata')):
            self.assertEqual(remote_mcp.Server(None, client_platform='linux-x86_64').client_platform, 'linux-x86_64')
            for target in ('win32-', 'win32-x86', 'win32-unknown', 'darwin-unknown'):
                with self.subTest(target=target), self.assertRaises(ValueError):
                    remote_mcp.Server(None, client_platform=target)

    def test_source_and_actual_release_run_content_checkpoint_in_minimal_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            payload = root / 'release'
            release(ROOT, payload, '0.5.6', 'personal-content-https', native_platform())
            environment = {'HOME': str(root), 'CODEX_HOME': str(root / 'codex')}
            if os.name == 'nt':
                environment['SystemRoot'] = os.environ['SystemRoot']
            for package in (ROOT, payload):
                with self.subTest(package=package.name):
                    run = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'tests/fixtures/checkpoint_probe.py'), str(package)],
                        env=environment, capture_output=True, text=True, timeout=30)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertEqual(json.loads(run.stdout)['checkpoint'], native_platform())

    def test_platform_discovery_cannot_escape_the_shared_boundary(self):
        for source in ('import platform as p\np.machine()', 'from platform import machine as cpu\ncpu()', 'from platform import *\nmachine()', 'import os as system\nsystem.uname()'):
            with self.subTest(source=source), self.assertRaises(ValueError):
                check_platform_boundary('adapters/codex/example.py', source)
            with self.subTest(root_source=source), self.assertRaises(ValueError):
                check_platform_boundary('connect.py', source)
        check_platform_boundary('scripts/brain_lock.py', 'import platform\nplatform.machine()')


import content_install as ci
from test_content_outbox import POLICY
from test_package_tool_binding import old_contract


class HistoricalPackageTools(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.grant={'scopes':['read','put'],'artifact_types':POLICY['kinds'],'ids':POLICY['ids']}

    def test_target_contract_is_independent_of_newer_inspecting_source(self):
        package=self.root/'old-package'
        manifest=release(ROOT,package,'0.6.2','personal-content-https',native_platform())
        old_contract(manifest,package)
        with patch.object(ci,'enabled_tools',side_effect=AssertionError('Updater tool contract must not select old package tools')):
            tools=ci.package_tools(package,POLICY,self.grant)
        self.assertEqual(len(tools),9)
        self.assertNotIn('memory_recall',tools)
        self.assertIn('memory_query',tools)

    def test_future_package_addition_needs_no_version_or_name_allowlist(self):
        package=self.root/'future-package'
        release(ROOT,package,'0.9.0','personal-content-https',native_platform())
        file=package/'adapters/codex/content_mcp.py'
        raw=file.read_text().replace('    return tools\n',"    tools.append({'name':'synthetic_future_read'})\n    return tools\n",1)
        file.write_text(raw)
        tools=ci.package_tools(package,POLICY,self.grant)
        self.assertIn('synthetic_future_read',tools)
        self.assertIn('memory_recall',tools)
        self.assertEqual(len(tools),11)

    def test_probe_failure_is_bounded_and_does_not_accept_updater_fallback(self):
        package=self.root/'old-package'
        manifest=release(ROOT,package,'0.6.2','personal-content-https',native_platform())
        old_contract(manifest,package)
        with patch.object(ci.subprocess,'run',return_value=type('Result',(),{'returncode':1,'stdout':b''})()):
            with self.assertRaises(ci.InstallBlocked):
                ci.package_tools(package,POLICY,self.grant)


if __name__ == '__main__':
    unittest.main()
