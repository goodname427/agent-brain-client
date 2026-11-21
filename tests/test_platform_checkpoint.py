"""Exercise the actual MCP checkpoint under the launcher's minimal environment."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)] + [str(ROOT / n) for n in ('src', 'src', 'src')]
import dev.integration.checkpoint as connect
import agent_brain_client.interfaces.mcp.topics as remote_mcp
from agent_brain_client.adapters.packages.client import CAPABILITIES
from agent_brain_client.adapters.platforms.filesystem import native_platform
from dev.build.package import export, release
from dev.checks.source import check_platform_boundary


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
root=Path(sys.argv[1]);sys.path[:0]=[str(root/n) for n in ('src','src')]
assert platform.machine()==''
from agent_brain_client.interfaces.mcp.topics import Server
from agent_brain_client.adapters.platforms.filesystem import require_platform
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
        check_platform_boundary('src/agent_brain_client/adapters/platforms/filesystem.py', 'import platform\nplatform.machine()')


import agent_brain_client.adapters.installation.installation as ci
from test_content_outbox import POLICY


class VerifiedPackageTools(unittest.TestCase):
    catalog_path = 'src/agent_brain_client/application/tools/catalog.py'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.grant = {
            'scopes': ['read', 'put'],
            'artifact_types': POLICY['kinds'],
            'ids': POLICY['ids'],
        }

    def write_candidate_source(self, package, name, source):
        data = source.encode('utf-8')
        (package / name).write_bytes(data)
        # Tool discovery compares the pinned digests, so both manifests must
        # describe the revised fixture bytes.
        digest = hashlib.sha256(data).hexdigest()
        for manifest_name in ('export-manifest.json', 'releases/manifest.json'):
            path = package / manifest_name
            manifest = json.loads(path.read_bytes())
            self.assertIn(name, manifest['files'])
            manifest['files'][name] = digest
            path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')

    def test_future_package_addition_needs_no_version_or_name_allowlist(self):
        package = self.root / 'future-package'
        release(ROOT, package, '0.9.0', 'personal-content-https', native_platform())
        before = set(ci.package_tools(package, POLICY, self.grant))
        source = (package / self.catalog_path).read_text(encoding='utf-8')
        updated = source.replace(
            '    return tools\n',
            "    tools.append({'name': 'synthetic_future_read'})\n    return tools\n",
            1,
        )
        self.write_candidate_source(package, self.catalog_path, updated)
        tools = ci.package_tools(package, POLICY, self.grant)
        self.assertIn('synthetic_future_read', tools)
        self.assertIn('memory_query', tools)
        self.assertEqual(set(tools), before | {'synthetic_future_read'})

    def test_same_catalog_with_different_target_contract_uses_target_definitions(self):
        package = self.root / 'changed-contract'
        release(ROOT, package, '0.9.0', 'personal-content-https', native_platform())
        name = 'src/agent_brain_client/domain/authorization.py'
        source = (package / name).read_text(encoding='utf-8')
        updated = source.replace(
            'return methods(scopes) - HOOK_METHODS',
            "return methods(scopes) - HOOK_METHODS - {'content_put'}",
        )
        self.assertNotEqual(source, updated)
        self.write_candidate_source(package, name, updated)
        self.assertNotIn('content_put', ci.package_tools(package, POLICY, self.grant))

    def test_probe_failure_is_bounded_and_does_not_accept_updater_fallback(self):
        package = self.root / 'old-package'
        release(ROOT, package, '0.9.0', 'personal-content-https', native_platform())
        source = (package / self.catalog_path).read_text(encoding='utf-8')
        self.write_candidate_source(
            package, self.catalog_path, source + '\n# Different verified package source\n',
        )
        failed_probe = subprocess.CompletedProcess(args=[], returncode=1, stdout=b'')
        with patch.object(ci.subprocess, 'run', return_value=failed_probe):
            with self.assertRaisesRegex(
                ci.InstallBlocked, 'Verified package tool contract unavailable',
            ):
                ci.package_tools(package, POLICY, self.grant)

    def test_reviewed_windows_catalog_reuse_requires_exact_package_and_catalog_pins(self):
        package = self.root / "reviewed-windows-package"
        release(ROOT, package, "0.8.25", "personal-content-https", "win32-amd64")
        name = "src/agent_brain_client/application/control.py"
        self.write_candidate_source(package, name, (package / name).read_text() + "\n# Different recall code\n")
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"")

        def package_sha():
            files = json.loads((package / "export-manifest.json").read_bytes())["files"]
            return ci.sha(ci.contract.encode(files))

        reviewed = package_sha()
        with (
            patch.object(ci, "_WINDOWS_0825_PACKAGE_SHA", reviewed),
            patch.object(ci.subprocess, "run", return_value=failed) as child,
        ):
            tools = ci.package_tools(package, POLICY, self.grant, inspected_windows_package_sha=reviewed)
            self.assertIn("content_put", tools)
            child.assert_not_called()
            for unknown in (None, "0" * 64):
                with self.subTest(unknown=unknown), self.assertRaisesRegex(
                    ci.InstallBlocked, "Verified package tool contract unavailable",
                ):
                    ci.package_tools(package, POLICY, self.grant, inspected_windows_package_sha=unknown)

        source = (package / self.catalog_path).read_text()
        self.write_candidate_source(package, self.catalog_path, source + "\n# New catalog revision\n")
        revised = package_sha()
        # Even an exact approved inventory must match the inspector's catalog.
        with (
            patch.object(ci, "_WINDOWS_0825_PACKAGE_SHA", revised),
            patch.object(ci.subprocess, "run", return_value=failed) as child,
            self.assertRaisesRegex(ci.InstallBlocked, "Verified package tool contract unavailable"),
        ):
            ci.package_tools(package, POLICY, self.grant, inspected_windows_package_sha=revised)
        child.assert_called_once()

    def test_package_check_rejects_catalog_with_stale_export_digest(self):
        package = self.root / 'changed-after-export'
        export(ROOT, package)
        policy = {'capabilities': CAPABILITIES}
        ci.package_check(package, policy)
        catalog = package / self.catalog_path
        catalog.write_bytes(catalog.read_bytes() + b'\n# Changed after export\n')
        with self.assertRaisesRegex(ci.InstallBlocked, 'Candidate package changed'):
            ci.package_check(package, policy)


if __name__ == '__main__':
    unittest.main()
