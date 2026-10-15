"""Exact public allowlist and client-only registration, without native changes."""
import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'));sys.path.insert(0,str(ROOT/'adapters/codex'));sys.path.insert(0, str(ROOT / 'scripts'))
import build_client as build_client_export
spec = importlib.util.spec_from_file_location('remote_connect', ROOT / 'connect.py')
connector = importlib.util.module_from_spec(spec);spec.loader.exec_module(connector)


class FakeClient:
    def __init__(self, *args):
        pass

    def request(self, request):
        return {'request_id': request['request_id'], 'result': {'core_api_version': 1, 'status': 'not-configured'}}


class ClientExport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_export_has_only_exact_client_files_and_no_git_or_core(self):
        output = self.base / 'public-client'
        manifest = build_client_export.export(ROOT, output)
        names = {str(p.relative_to(output)) for p in output.rglob('*') if p.is_file()}
        self.assertEqual(names, set(build_client_export.FILES.values()) | {'export-manifest.json'})
        self.assertFalse((output / '.git').exists());self.assertFalse((output / 'memory').exists())
        self.assertFalse((output / 'scripts/brain_core.py').exists())
        self.assertFalse(manifest['core_exported']);self.assertFalse(manifest['git_history_copied'])
        command = [sys.executable, '-B', str(output / 'connect.py'), '--agent', 'codex', '--device-home', str(self.base / 'empty-home')]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'server-authorization-required')

    def test_notify_mux_is_in_candidate_manifest_and_hash_matches_file(self):
        output = self.base / 'notify-client'
        manifest = build_client_export.export(ROOT, output, ssh_content_candidate=True)
        relative = 'adapters/codex/codex_notify_mux.py'
        self.assertIn(relative, manifest['files'])
        self.assertEqual(manifest['files'][relative], hashlib.sha256((output / relative).read_bytes()).hexdigest())
        self.assertEqual((output / relative).read_bytes(), (ROOT / relative).read_bytes())
        self.assertEqual(manifest['release_policy_scope_delta'], {'added': [relative], 'removed': []})
        self.assertEqual(manifest['production_release_policy_scope_compatibility'], 'not-checked-by-build')
        self.assertFalse(manifest['production_release_policy_compatible'])
        self.assertIn('production_release_policy_compatibility_reason', manifest)

    def test_registration_is_idempotent_and_service_drift_is_preserved(self):
        home = self.base / 'client-home';token = self.base / 'client.token'
        with patch.object(connector, 'RemoteClient', FakeClient):
            first = connector.connect(home, 'http://127.0.0.1:8765', token, True)
            self.assertTrue(first['client_metadata_written']);self.assertFalse(first['native_changed'])
            registry = home / '.config/agent-brain-client/device.json';before = registry.read_bytes()
            self.assertEqual(registry.stat().st_mode & 0o077, 0)
            repeat = connector.connect(home, apply=True)
            self.assertFalse(repeat['client_metadata_written']);self.assertEqual(registry.read_bytes(), before)
            blocked = connector.connect(home, 'http://127.0.0.1:9876', token, True)
            self.assertEqual(blocked['status'], 'service-change-review-required');self.assertEqual(registry.read_bytes(), before)

    def test_existing_native_binding_is_not_overwritten_or_contacted(self):
        home = self.base / 'old-client';legacy = home / '.config/agent-brain/device.json'
        legacy.parent.mkdir(parents=True);legacy.write_text('{"fixture":"old binding"}')
        with patch.object(connector, 'RemoteClient') as network:
            result = connector.connect(home, 'http://127.0.0.1:8765', self.base / 'token', True)
        self.assertEqual(result['status'], 'migration-review-required');network.assert_not_called()
        self.assertEqual(legacy.read_text(), '{"fixture":"old binding"}')
        self.assertFalse((home / '.config/agent-brain-client').exists())

    def test_export_preserves_existing_destination(self):
        target = self.base / 'existing';target.mkdir();(target / 'work').write_text('Keep this work')
        with self.assertRaises(ValueError):
            build_client_export.export(ROOT, target)
        self.assertEqual((target / 'work').read_text(), 'Keep this work')


if __name__ == '__main__':
    unittest.main()
