"""Portable inventory checks and an opt-in 0.8.24 update-engine handoff."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_brain_client.adapters.packages import client
from agent_brain_client.adapters.packages.releases import (
    ReleaseEngine,
    UpdateBlocked,
    atomic_json,
    digest,
    unique_manifest_object,
)
from agent_brain_client.adapters.platforms.filesystem import (
    MAC_PLATFORMS,
    WINDOWS_PLATFORMS,
    native_platform,
    private_directory,
)


ROOT = Path(__file__).resolve().parents[1]
BASELINE = "e1f14dc3b66d50e9f52e7204b8b6e8a5c802eb1a"
PLATFORMS = ("win32-amd64", "darwin-arm64")
NEW = "src/agent_brain_client/application/memory/bridge_fixture.py"
ENTRY = "src/agent_brain_client/interfaces/mcp/content.py"
WIN_CREDENTIAL = "src/agent_brain_client/adapters/platforms/windows/native.py"
MAC_CREDENTIAL = (
    "src/agent_brain_client/adapters/platforms/macos/native/CredentialStore.swift"
)


def policy(platform, files):
    credential = WIN_CREDENTIAL if platform in WINDOWS_PLATFORMS else MAC_CREDENTIAL
    return {
        "channel": "stable",
        "core_api": 1,
        "state_schema": 1,
        "capabilities": client.HTTPS_CONTENT_CAPABILITIES,
        "platform": platform,
        "file_prefixes": list(client.https_files(platform)),
        "runtime": {"credential_source_sha256": files[credential]},
    }


def manifest(files, version="0.8.26"):
    return {
        "schema": 1,
        "version": version,
        "channel": "stable",
        "core_api": 1,
        "state_schema": 1,
        "python_min": "3.11",
        "capabilities": client.HTTPS_CONTENT_CAPABILITIES,
        "files": files,
        "migrations": [],
    }


class ReleaseBridgeInventory(unittest.TestCase):
    def test_existing_required_and_new_python_members(self):
        for platform, expected in zip(PLATFORMS, (185, 186)):
            with self.subTest(platform=platform):
                files = dict.fromkeys(client.https_files(platform), "a" * 64)
                self.assertEqual(len(files), expected)
                approved = policy(platform, files)
                with patch.object(client, "require_platform"):
                    adapter = client.ClientPackage(approved)
                engine = ReleaseEngine(".", approved, None, adapter, {})
                engine.validate(manifest(files), None)
                extended = dict(files, **{NEW: "b" * 64})
                engine.validate(manifest(extended), {"version": "0.8.25"})
                self.assertEqual(set(adapter.package_files(manifest(extended))), set(extended))

                outside = (
                    "src/agent_brain_client/adapters/platforms/new_helper.py",
                    "src/agent_brain_client/domain/authorization.json",
                    "src/agent_brain_client/domain/../outside.py",
                    "src/agent_brain_client/domain/Bridge_Fixture.py",
                    "src/agent_brain_client/domain/con.py",
                    "src/agent_brain_client/domain/unlisted_package/feature.py",
                    "src/agent_brain_client/domain/authorization/__init__.py",
                )
                for name in outside:
                    with self.subTest(path=name), self.assertRaises(UpdateBlocked):
                        engine.validate(manifest(dict(files, **{name: "b" * 64})), None)

                missing = dict(extended)
                del missing[ENTRY]
                with self.assertRaisesRegex(UpdateBlocked, "Required Client package"):
                    engine.validate(manifest(missing), None)
                with self.assertRaisesRegex(UpdateBlocked, "Downgrade"):
                    engine.validate(manifest(extended), {"version": "0.8.27"})
                changed = dict(manifest(extended), capabilities=["synthetic-only"])
                with self.assertRaisesRegex(UpdateBlocked, "Permission scope"):
                    engine.validate(changed, None)

    def test_duplicate_json_member_is_rejected(self):
        with self.assertRaisesRegex(UpdateBlocked, "Duplicate release manifest"):
            json.loads('{"files":{"module.py":"a","module.py":"b"}}',
                       object_pairs_hook=unique_manifest_object)

    def test_expanded_cache_rejects_unlisted_python_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            platform = PLATFORMS[0]
            files = dict.fromkeys(client.https_files(platform), "a" * 64)
            with patch.object(client, "require_platform"):
                adapter = client.ClientPackage(policy(platform, files))
            for name in (*files, NEW):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"# synthetic\n")
            extended = dict(files, **{NEW: "b" * 64})
            adapter.preflight(root, manifest(extended))
            (root / "src/agent_brain_client/domain/unlisted.py").write_bytes(b"")
            with self.assertRaisesRegex(UpdateBlocked, "membership changed"):
                adapter.preflight(root, manifest(extended))


ENGINE = """
import json
import sys
from pathlib import Path

root, policy_path = map(Path, sys.argv[1:])
sys.path.insert(0, str(root / "src"))
from agent_brain_client.adapters.packages.client import ClientPackage
from agent_brain_client.adapters.packages.releases import GitReleaseSource, ReleaseEngine, UpdateBlocked

policy = json.loads(policy_path.read_bytes())
adapter = ClientPackage(policy)
engine = ReleaseEngine(policy["cache"], policy, GitReleaseSource(policy["repository"], policy), adapter, {})
try:
    result = engine.check()
except UpdateBlocked as error:
    result = {"status": "blocked", "error": str(error), "active": engine.active()}
print(json.dumps(result))
"""


class ReleaseBridgeHandoff(unittest.TestCase):
    def test_old_bridge_expanded_and_rollback(self):
        location = os.environ.get("AGENT_BRAIN_BRIDGE_BASELINE")
        platform = native_platform()
        if not location or platform not in MAC_PLATFORMS + WINDOWS_PLATFORMS:
            self.skipTest("Requires a native Mac/Windows runner and the 0.8.24 checkout")
        old = Path(location).resolve()

        environment = dict(
            os.environ,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_COUNT="0",
            GIT_TERMINAL_PROMPT="0",
        )

        def run(*args):
            return subprocess.check_output(
                args, stdin=subprocess.DEVNULL, env=environment
            )

        def git(repo, *args):
            return run("git", "-C", str(repo), *args).decode().strip()

        self.assertEqual(git(old, "rev-parse", "HEAD"), BASELINE)
        self.assertEqual(git(old, "status", "--porcelain"), "")
        with tempfile.TemporaryDirectory() as temp:
            base = private_directory(Path(temp).resolve() / "private")
            origin = base / "origin"
            origin.mkdir()
            git(origin, "init", "-b", "synthetic-release")
            git(origin, "config", "user.name", "Synthetic release fixture")
            git(origin, "config", "user.email", "fixture@example.invalid")
            checkout = base / "checkout"
            policy_file = base / "policy.json"
            cache = base / "cache"

            def publish(source, version, *, add=False, fail_health=False):
                output = base / ("build-" + version)
                run(
                    sys.executable, "-I", "-B", str(source / "dev/build/package.py"),
                    "--output", str(output), "--version", version,
                    "--mode", "personal-content-https", "--platform", platform,
                )
                path = output / "releases/manifest.json"
                built = json.loads(path.read_bytes())
                if add:
                    module = output / NEW
                    module.write_bytes(b"VALUE = 42\n")
                    original = (output / ENTRY).read_bytes()
                    before = b"def self_check():\n"
                    self.assertEqual(original.count(before), 1)
                    assertion = (
                        b"    from agent_brain_client.application.memory.bridge_fixture import VALUE\n"
                        + (b"    assert VALUE == 0\n" if fail_health else b"    assert VALUE == 42\n")
                    )
                    (output / ENTRY).write_bytes(
                        original.replace(before, before + assertion)
                    )
                    for member in (NEW, ENTRY):
                        built["files"][member] = digest((output / member).read_bytes())
                path.write_text(json.dumps(built), encoding="utf-8")
                for member in (*built["files"], "releases/manifest.json"):
                    target = origin / member
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(output / member, target)
                git(origin, "add", "--all")
                git(origin, "commit", "-m", "Synthetic Client " + version)
                return git(origin, "rev-parse", "HEAD"), built

            def update(source):
                return json.loads(run(
                    sys.executable, "-I", "-B", "-c", ENGINE, str(source), str(policy_file)
                ))

            anchor, previous = publish(old, "0.8.24")
            git(base, "clone", str(origin), str(checkout))
            approved = dict(
                policy(platform, previous["files"]),
                origin=git(checkout, "remote", "get-url", "origin"),
                ref="refs/heads/synthetic-release",
                anchor_commit=anchor,
                repository=str(checkout),
                cache=str(cache),
            )
            atomic_json(policy_file, approved)
            self.assertEqual(update(old)["active"]["commit"], anchor)

            bridge, same = publish(ROOT, "0.8.25")
            self.assertEqual(set(same["files"]), set(previous["files"]))
            self.assertEqual(update(old)["active"]["commit"], bridge)

            expanded, future = publish(ROOT, "0.8.26", add=True)
            self.assertEqual(future["files"].keys() - same["files"].keys(), {NEW})
            old_result = update(old)
            self.assertEqual(old_result["status"], "blocked")
            self.assertIn("File outside approved runtime scope", old_result["error"])
            self.assertEqual(old_result["active"]["commit"], bridge)

            activated = update(ROOT)
            self.assertEqual(activated["status"], "updated")
            self.assertEqual(activated["active"]["commit"], expanded)

            publish(ROOT, "0.8.27", add=True, fail_health=True)
            failure = update(ROOT)
            self.assertEqual(failure["status"], "blocked")
            self.assertIn("Client health check failed", failure["error"])
            self.assertEqual(failure["active"], activated["active"])
            journal = cache / "journal.json"
            rolled = json.loads(journal.read_bytes())
            self.assertEqual(rolled["phase"], "rolled-back")

            atomic_json(cache / "active.json", rolled["target"])
            atomic_json(journal, dict(rolled, phase="activated"))
            origin.rename(base / "offline-origin")
            recovery = update(ROOT)
            self.assertEqual(recovery["status"], "offline")
            self.assertEqual(recovery["active"], activated["active"])
            self.assertEqual(
                json.loads(journal.read_bytes())["phase"], "rolled-back-interrupted"
            )
            self.assertEqual(json.loads(policy_file.read_bytes()), approved)
