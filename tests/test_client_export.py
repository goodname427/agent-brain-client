"""Exact public allowlist and client-only registration, without native changes."""
from contextlib import redirect_stdout
import importlib.util
import io
import hashlib
import json
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0, str(ROOT / 'src'))
import dev.build.package as build_client_export
from agent_brain_client.adapters.packages.layout import source_python_members
spec = importlib.util.spec_from_file_location('remote_connect', ROOT / 'dev/integration/checkpoint.py')
connector = importlib.util.module_from_spec(spec);spec.loader.exec_module(connector)


class FakeClient:
    def __init__(self, *args):
        pass

    def request(self, request):
        return {'request_id': request['request_id'], 'result': {'core_api_version': 1, 'status': 'not-configured'}}


class ClientExport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

    def test_export_has_only_exact_client_files_and_no_git_or_core(self):
        output = self.base / 'public-client'
        manifest = build_client_export.export(ROOT, output)
        names = {str(p.relative_to(output)) for p in output.rglob('*') if p.is_file()}
        self.assertEqual(names, set(build_client_export.FILES.values()) | set(source_python_members(ROOT)) | {'export-manifest.json'})
        self.assertFalse((output / '.git').exists());self.assertFalse((output / 'memory').exists())
        self.assertFalse((output / 'scripts/brain_core.py').exists())
        self.assertFalse(manifest['core_exported']);self.assertFalse(manifest['git_history_copied'])
        command = [sys.executable, '-B', str(output / 'src/agent_brain_client/interfaces/mcp/topics.py'), '--self-check']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'client_self_check':'passed','core_imported':False,'native_changed':False})

    def test_deferred_notify_mux_is_not_in_candidate_manifest(self):
        output = self.base / 'notify-client'
        manifest = build_client_export.export(ROOT, output, ssh_content_candidate=True)
        relative = 'src/agent_brain_client/adapters/harnesses/codex/notify.py'
        self.assertNotIn(relative, manifest['files'])
        self.assertFalse((output / relative).exists())
        self.assertEqual(manifest['release_policy_scope_delta'], {'added': sorted(set(build_client_export.FILES) | set(source_python_members(ROOT))), 'removed': []})
        self.assertEqual(manifest['production_release_policy_scope_compatibility'], 'current-layout-reinstall')
        self.assertFalse(manifest['production_release_policy_compatible'])
        self.assertIn('production_release_policy_compatibility_reason', manifest)

    def test_committed_031_release_manifest_remains_frozen_during_source_development(self):
        committed_bytes = (ROOT / 'releases/manifest.json').read_bytes()
        # Published 0.3.1 remains immutable while unreleased source advances.
        # Refreshing its hashes would silently replace an existing version.
        self.assertEqual(hashlib.sha256(committed_bytes).hexdigest(),
                         'ecc2058e14aea2b9b1ddc0c7df185778c8bba0525596c7f96b2b07eee0de8106')
        manifest = json.loads(committed_bytes)
        self.assertEqual(manifest['version'], '0.3.1')
        self.assertNotIn('commit', manifest)

    def test_unpublished_https_export_matches_current_source_without_relabeling_release(self):
        frozen = (ROOT / 'releases/manifest.json').read_bytes()
        output = self.base / 'unpublished-https-source'
        generated = build_client_export.export(ROOT, output, https_content_candidate=True)
        self.assertEqual(generated['review_status'], 'unpublished-candidate')
        self.assertFalse(generated['production_release_policy_compatible'])
        self.assertFalse(generated['native_install_wired'])
        self.assertNotIn('version', generated)
        from agent_brain_client.adapters.packages.client import HTTPS_CONTENT_CLIENT_FILES
        from agent_brain_client.adapters.packages.layout import source_python_members
        self.assertEqual(
            set(generated["files"]),
            set(HTTPS_CONTENT_CLIENT_FILES) | set(source_python_members(ROOT)),
        )
        for name, digest in generated['files'].items():
            source = ROOT / 'docs/reference' / name if name in {'ADAPTERS.md', 'PROTOCOL.md'} else ROOT / name
            self.assertEqual(digest, hashlib.sha256(source.read_bytes()).hexdigest(), name)
            self.assertEqual((output / name).read_bytes(), source.read_bytes(), name)
        self.assertFalse((output / 'releases/manifest.json').exists())
        self.assertEqual((ROOT / 'releases/manifest.json').read_bytes(), frozen)

    def test_https_export_selects_committed_modules_and_excludes_local_python(self):
        from agent_brain_client.adapters.packages import build
        from agent_brain_client.adapters.packages.client import https_files

        source = self.base / "source"
        ordinary = "src/agent_brain_client/domain/contract.py"
        domain = "src/agent_brain_client/domain/__init__.py"
        initial_files = {ordinary, domain}
        for name in set(https_files("darwin-arm64")) | initial_files:
            relative = build.PACKAGE_SOURCE_PATHS.get(name, name)
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"# Synthetic package fixture\n")

        def git(*args):
            return subprocess.check_output(["git", "-C", str(source), *args])

        git("init", "-q")
        committed = "src/agent_brain_client/domain/fixture_feature.py"
        ignored = "src/agent_brain_client/domain/fixture_local.py"
        (source / committed).write_bytes(b"VALUE = 42\n")
        git("add", "--all")
        git("-c", "user.name=Synthetic", "-c", "user.email=fixture@example.invalid",
            "commit", "-qm", "Synthetic runtime source")
        (source / ignored).write_bytes(b"VALUE = 0\n")
        with (source / ".git/info/exclude").open("a") as exclude:
            exclude.write("\n/" + ignored + "\n")
        self.assertEqual(git("status", "--porcelain"), b"")

        archive_source = self.base / "archive-source"
        shutil.copytree(source, archive_source, ignore=shutil.ignore_patterns(".git", "fixture_local.py"))
        for platform in ("darwin-arm64", "win32-amd64"):
            with self.subTest(platform=platform):
                inventories = []
                for layout in (source, archive_source):
                    output = self.base / (layout.name + "-" + platform)
                    arguments = ["package.py", "--output", str(output), "--platform", platform]
                    with (
                        patch.object(build, "ROOT", layout),
                        patch.object(build_client_export, "ROOT", layout),
                        patch.object(sys, "argv", arguments),
                        redirect_stdout(io.StringIO()) as summary,
                    ):
                        build_client_export.main()
                    manifest = json.loads(summary.getvalue())
                    self.assertEqual(set(manifest["files"]), set(https_files(platform)) | initial_files | {committed})
                    self.assertFalse((output / ignored).exists())
                    self.assertEqual((output / committed).read_bytes(), b"VALUE = 42\n")
                    inventories.append(manifest["files"])
                self.assertEqual(inventories[0], inventories[1])

        moved = "src/agent_brain_client/domain/fixture_renamed.py"
        git("mv", "--", committed, moved)
        git("rm", "--", ordinary)
        git("-c", "user.name=Synthetic", "-c", "user.email=fixture@example.invalid",
            "commit", "-qm", "Move and remove ordinary modules")
        output = self.base / "edited"
        with patch.object(build, "ROOT", source):
            manifest = build.export(source, output, https_content_candidate=True, target_platform="darwin-arm64")
        self.assertEqual(set(manifest["files"]), set(https_files("darwin-arm64")) | {domain, moved})

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
