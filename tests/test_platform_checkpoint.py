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
sys.path[:0] = [str(ROOT / n) for n in ('scripts', 'adapters/codex', 'tools')]
import remote_mcp
from brain_lock import native_platform
from build_client import release
from check_source import check_platform_boundary


class PlatformCheckpoint(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt' and sys.version_info[:3] <= (3, 11, 9), 'Historical Windows CPython 3.11 environment')
    def test_python311_blank_stdlib_architecture_still_reaches_real_checkpoint(self):
        code = """import json,platform,sys
from pathlib import Path
root=Path(sys.argv[1]);sys.path[:0]=[str(root/n) for n in ('scripts','adapters/codex')]
assert platform.machine()==''
from remote_mcp import Server
from brain_lock import require_platform
assert Server(None,'0.5.4').client_platform==require_platform('checkpoint-regression')
print(json.dumps({'stdlib_machine_empty':True,'fixed_checkpoint':Server(None,'0.5.4').client_platform}))
"""
        run = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(ROOT)],
            env={'SystemRoot': os.environ['SystemRoot']}, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(json.loads(run.stdout)['stdlib_machine_empty'])

    def test_default_checkpoint_uses_shared_detector_and_explicit_metadata_stays_validated(self):
        with patch.object(remote_mcp, 'native_platform', return_value='win32-arm64') as detect:
            self.assertEqual(remote_mcp.Server(None, '0.5.4').client_platform, 'win32-arm64')
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
            release(ROOT, payload, '0.5.4', 'personal-content-https', native_platform())
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
        check_platform_boundary('scripts/brain_lock.py', 'import platform\nplatform.machine()')


if __name__ == '__main__':
    unittest.main()
