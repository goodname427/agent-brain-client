"""Gateway process transport with synthetic, isolated child processes."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_brain_client.interfaces.cli import codebuddy
from agent_brain_client.runtime import host_binding

ROOT = Path(__file__).resolve().parents[1]


class Gateway(unittest.TestCase):
    def test_windows_inherits_streams_and_preserves_backend_exit(self):
        command = {
            'command': 'synthetic-python',
            'args': ['-I', 'synthetic-launcher.py'],
            'env': {'SYNTHETIC_CLIENT': '1'},
        }
        execute = Mock(side_effect=AssertionError('Windows must not use execve'))
        runner = Mock(return_value=SimpleNamespace(returncode=37))
        with patch.object(host_binding, 'backend', return_value=(command, {})), patch.object(host_binding.sys, 'platform', 'win32'):
            result = host_binding.execute_mcp('synthetic-home', object(), execute, runner)
        self.assertEqual(result, 37)
        execute.assert_not_called()
        args, kwargs = runner.call_args
        self.assertEqual(args, (['synthetic-python', '-I', 'synthetic-launcher.py'],))
        self.assertEqual(kwargs['env']['SYNTHETIC_CLIENT'], '1')
        self.assertFalse(kwargs['check'])
        self.assertEqual(set(kwargs), {'env', 'check'})

    def test_serve_returns_gateway_exit_code(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(sys, 'argv', ['codebuddy.py', 'serve', '--device-home', str(Path(temp).resolve())]), patch.object(codebuddy, 'verify_adapter'), patch.object(host_binding, 'load_core'), patch.object(host_binding, 'execute_mcp', return_value=37):
            self.assertEqual(codebuddy.main(), 37)

    def test_subprocess_gateway_preserves_real_stdio_and_eof(self):
        child = (
            "import os, sys\n"
            "payload = sys.stdin.buffer.read()\n"
            "sys.stdout.buffer.write(payload)\n"
            "sys.stderr.write(os.environ['SYNTHETIC_CLIENT'])\n"
            "raise SystemExit(37)\n"
        )
        command = {
            'command': sys.executable,
            'args': ['-I', '-B', '-c', child],
            'env': {'SYNTHETIC_CLIENT': 'synthetic-stderr'},
        }
        gateway = (
            'import sys\n'
            f'sys.path.insert(0, {str(ROOT / "src")!r})\n'
            'from agent_brain_client.runtime import host_binding\n'
            f'host_binding.backend = lambda *args: ({command!r}, {{}})\n'
            'host_binding.sys.platform = "win32"\n'
            'raise SystemExit(host_binding.execute_mcp("synthetic-home", None))\n'
        )
        payload = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}, separators=(',', ':')).encode() + b'\n'
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, HOME=temp, USERPROFILE=temp, CODEX_HOME=temp)
            for attempt in range(6):
                with self.subTest(attempt=attempt):
                    result = subprocess.run([sys.executable, '-I', '-B', '-c', gateway], input=payload, capture_output=True, env=env, timeout=20)
                    self.assertEqual(result.returncode, 37, result.stderr)
                    self.assertEqual(result.stdout, payload)
                    self.assertEqual(result.stderr, b'synthetic-stderr')
