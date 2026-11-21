"""Exact released Windows export update under an isolated synthetic user."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from agent_brain_client.adapters.platforms.filesystem import native_platform, private_directory
from agent_brain_client.adapters.storage import install_state, private_files
from agent_brain_client.interfaces.cli import install as install_cli
from agent_brain_client.runtime import dependencies as runtime_dependencies
import test_windows_native as native_fixture


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Native Windows APIs required")
class ReleasedWindowsUpdate(unittest.TestCase):
    setUp = native_fixture.WindowsNative.setUp

    def test_0825_registered_export_updates_despite_old_describe_tools_timeout(self):
        import io
        import tarfile
        from unittest.mock import patch
        from agent_brain_client.adapters.installation import installation
        from agent_brain_client.adapters.packages.client import ClientReleases
        from agent_brain_client.adapters.packages.releases import GitReleaseSource
        from agent_brain_client.adapters.server.https import HTTPSContentClient
        from agent_brain_client.runtime import installation as installer
        from agent_brain_client.runtime.host_maintenance import HostMaintenance
        from dev.build.package import release

        old_commit = "20efb22488279ee42c67f6e4f55b0befa5a0bb6a"
        old_setup = "aefa7ba1387ebdea852d469ced83b82fc27f1a98"
        ci = os.environ.get("GITHUB_ACTIONS") == "true"
        if native_platform() != "win32-amd64":
            if ci:
                self.fail("The exact 0.8.25 export check requires Windows AMD64")
            self.skipTest("The original 0.8.25 export targets Windows AMD64")

        def has_release(repo, commit):
            got = subprocess.run(
                ["git", "cat-file", "-e", commit + "^{commit}"], cwd=repo,
                capture_output=True, timeout=10,
            )
            return got.returncode == 0

        repository = ROOT
        if not all(has_release(repository, commit) for commit in (old_commit, old_setup)):
            if not ci:
                self.skipTest("The original release objects are absent from this checkout")
            # CI uses a shallow checkout. Fetch only the two public read-only
            # fixtures into a disposable repository, outside the source checkout.
            repository = self.root / "published history"
            repository.mkdir()
            git_env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
            subprocess.run(["git", "init", "--quiet"], cwd=repository,
                           env=git_env, capture_output=True, check=True, timeout=15)
            fetched = subprocess.run(
                ["git", "fetch", "--depth=1", "--no-tags",
                 "https://github.com/goodname427/agent-brain-client.git",
                 "refs/tags/client-0.8.25-windows", "refs/tags/installer-0.8.26-setup"],
                cwd=repository, env=git_env, capture_output=True, timeout=45,
            )
            self.assertEqual(fetched.returncode, 0, "Cannot fetch the two public historical fixtures")
            for commit in (old_commit, old_setup):
                self.assertTrue(has_release(repository, commit), "Published history fixture commit differs")

        def archive(commit, output):
            raw = subprocess.check_output(["git", "archive", commit], cwd=repository, timeout=30)
            with tarfile.open(fileobj=io.BytesIO(raw)) as packed:
                # Both inputs are the exact public Git commits above.
                packed.extractall(output, filter="data")

        old_export = self.root / "official old export"
        old_entry = self.root / "official setup"
        archive(old_commit, old_export)
        archive(old_setup, old_entry)
        candidate = self.root / "offline candidate"
        release(ROOT, candidate, "0.8.27", data_mode="personal-content-https",
                target_platform=native_platform())

        def release_row(commit, package):
            manifest = json.loads((package / "releases/manifest.json").read_bytes())
            return commit, manifest, lambda name: (package / name).read_bytes()

        native = self.home / "Codex user home"
        native.mkdir()
        state = self.home / ".config/agent-brain-client"
        shared = private_directory(state / "shared-native")
        connection = {
            "schema": 1, "endpoint": "https://synthetic.invalid",
            "grant": {
                "adapter": "codex", "platform": native_platform(), "actor": "synthetic-owner",
                "scopes": ["read", "put"], "artifact_types": ["memory", "rule", "skill"],
                "ids": {kind: "personal-owner" for kind in ("memory", "rule", "skill")},
            },
            "release": {
                "origin": "https://127.0.0.1:1/synthetic",
                "ref": "refs/heads/releases/personal-https-windows-stable",
                "anchor_commit": old_commit,
            },
        }
        materials = runtime_dependencies.prepare_dependencies(self.home)

        def old_release():
            policy = state / "first-install/release-policy.json"
            channel = ClientReleases(policy, install_state.sha(policy.read_bytes()))
            result = channel.engine.check()
            result["active"] = channel.verified_active()
            return result

        with (
            patch.dict(os.environ, CODEX_HOME=str(native)),
            patch.object(GitReleaseSource, "latest", return_value=release_row(old_commit, old_export)),
        ):
            plan, pin, _ = installer.prepare_materials(
                materials, self.home, shared, connection,
                install_state.sha(installer.canonical(connection)),
                harness="codex", updater=old_release,
            )
            seeded = installation.apply(plan, pin, lambda value: None)
        self.assertTrue(seeded["installed"], seeded)
        pointer = state / "first-install/releases/active.json"
        previous = json.loads(pointer.read_bytes())
        self.assertEqual((previous["version"], previous["commit"]), ("0.8.25", old_commit))
        registration = state / "codex-mcp.json"
        original = private_files.private_json(registration)
        self.assertEqual(original["package"], previous["package"])
        self.assertEqual(original["package_sha256"], installation._WINDOWS_0825_PACKAGE_SHA)
        self.assertEqual(plan["mcp"]["command"], str(Path(materials["dependencies"]["python"]).resolve()))
        self.assertEqual(plan["mcp"]["args"][2], str(Path(previous["package"]) /
                         "src/agent_brain_client/interfaces/mcp/launcher.py"))
        self.assertNotEqual(Path(plan["codex_home"]), native)
        archive_marker = state / "synthetic-retained-archive"
        archive_marker.write_bytes(b"synthetic retained content")
        preserved = {p: p.read_bytes() for p in (pointer, registration, archive_marker)}
        artifact = Path(previous["package"]) / "src/agent_brain_client/interfaces/mcp/content.py"

        v0826 = """
import subprocess, sys
from pathlib import Path
source, old_entry, mode = sys.argv[1:]
sys.path.insert(0, str(Path(source) / "src"))
from agent_brain_client.interfaces.cli.install import main
from agent_brain_client.adapters.installation.installation import inspect
real_run = subprocess.run
calls = 0
def run(command, **kwargs):
    global calls
    if command[-1:] == ["--describe-tools"]:
        assert Path(command[-2]) == Path(old_entry)
        assert Path(command[0]).resolve() == Path(sys.executable).resolve()
        assert kwargs["timeout"] == 15
        calls += 1
        if mode == "timeout":
            raise subprocess.TimeoutExpired("synthetic-describe-timeout", 15)
    return real_run(command, **kwargs)
subprocess.run = run
if mode == "healthy":
    value = inspect(Path.home().resolve())
    assert value["registration_verified"], value
    assert calls == 1
else:
    sys.argv = ["install.py", "--action", "update", "--harness", "codex"]
    code = main()
    assert calls >= 1
    raise SystemExit(code)
"""
        environment = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                           CODEX_HOME=str(native))
        for mode in ("healthy", "timeout"):
            run = subprocess.run([sys.executable, "-I", "-B", "-c", v0826,
                                  str(old_entry), str(artifact), mode],
                                 env=environment, capture_output=True, text=True,
                                 encoding="utf-8", timeout=60)
            self.assertEqual(run.returncode, 0 if mode == "healthy" else 2, run.stderr)
            if mode == "timeout":
                report = json.loads(run.stdout)
                self.assertEqual(report["reason"], "policy-profile-bootstrap-review-required")
                self.assertFalse(report["channel_checked"])
            self.assertEqual({p: p.read_bytes() for p in preserved}, preserved)

        real_run = subprocess.run
        old_probe_calls = []

        def guarded_run(command, **kwargs):
            if command[-1:] == ["--describe-tools"] and Path(command[-2]) == artifact:
                old_probe_calls.append(True)
                raise subprocess.TimeoutExpired("synthetic-describe-timeout", 15)
            return real_run(command, **kwargs)

        # An integrity failure must still stop before tool declaration.
        before_artifact = artifact.read_bytes()
        artifact.write_bytes(before_artifact + b"\n")
        try:
            failed = installation.inspect(self.home)
            self.assertEqual(failed["reason"], "bootstrap-package-changed", failed)
        finally:
            artifact.write_bytes(before_artifact)

        # The channel provides local bytes only. Active package verification,
        # launcher handover, bootstrap checkpoint and activation are real.
        output = io.StringIO()
        candidate_commit = "b" * 40
        with (
            patch.object(Path, "home", return_value=self.home),
            patch.dict(os.environ, CODEX_HOME=str(native), USERPROFILE=str(self.home)),
            patch.object(sys, "argv", ["install.py", "--action", "update", "--harness", "codex"]),
            patch.object(sys, "stdout", output),
            patch.object(subprocess, "run", side_effect=guarded_run),
            patch.object(GitReleaseSource, "latest",
                         return_value=release_row(candidate_commit, candidate)),
            patch.object(HTTPSContentClient, "activate_identity",
                         side_effect=AssertionError("No real credential access")) as identity,
            patch.object(HostMaintenance, "install_sources", return_value={"native_changed": False}),
            patch("agent_brain_client.runtime.installation_completion.source_setup",
                  return_value={"collection_enabled": False}),
        ):
            code = install_cli.main()
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "client-update-updated", result)
        self.assertEqual(old_probe_calls, [])
        self.assertTrue(result["channel_checked"])
        self.assertTrue(result["active_package_changed"])
        self.assertTrue(result["bootstrap_registration_changed"])
        self.assertEqual(json.loads(pointer.read_bytes())["commit"], candidate_commit)
        self.assertEqual(archive_marker.read_bytes(), preserved[archive_marker])
        self.assertEqual(private_files.private_json(registration)["release_policy_sha256"],
                         original["release_policy_sha256"])
        identity.assert_not_called()
        self.assertTrue(installation.inspect(self.home)["registration_verified"])
