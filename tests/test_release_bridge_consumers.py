"""Opt-in formal-builder and registered-launcher bridge acceptance on macOS."""

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_brain_client.adapters.harnesses.codebuddy import configuration as buddy
from agent_brain_client.adapters.harnesses.codex import collection_installation as hooks
from agent_brain_client.adapters.installation import installation, launcher_update
from agent_brain_client.adapters.packages.client import ClientReleases
from agent_brain_client.adapters.packages.frozen import freeze_code, runtime_pins
from agent_brain_client.adapters.platforms.filesystem import native_platform, private_directory
from agent_brain_client.adapters.storage import collection, metadata
from dev.release import client as formal
from test_https_content import HTTPSContent
from test_release_bridge import BASELINE, ENTRY, NEW, ROOT


REF = "refs/heads/synthetic-release"
OFFICIAL_UPDATE = """
import sys
from pathlib import Path
from unittest.mock import patch

source, action = sys.argv[1:]
sys.path.insert(0, str(Path(source) / "src"))
from agent_brain_client.runtime import installation_completion
from agent_brain_client.interfaces.cli import install

sys.argv = ["install.py", "--action", action]
# The official entry otherwise reports Server-side source setup after update.
# The local update, pinned Git, launcher and native transactions remain real.
with patch.object(installation_completion, "source_setup", return_value={"synthetic": True}):
    raise SystemExit(install.main())
"""
FROZEN_IMPORT = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "src"))
from agent_brain_client.application.memory.bridge_fixture import VALUE
assert VALUE == 42
if sys.argv[2] == "codebuddy":
    from agent_brain_client.interfaces.cli import codebuddy
    codebuddy.verify_adapter()
"""


class ReleaseBridgeConsumers(unittest.TestCase):
    def test_formal_packages_and_installed_launcher_catch_up(self):
        baseline = os.environ.get("AGENT_BRAIN_BRIDGE_BASELINE")
        platform = native_platform()
        if not baseline or not platform.startswith("darwin-"):
            self.skipTest("Requires a native Mac and the exact 0.8.24 checkout")
        old = Path(baseline).resolve()
        environment = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }

        def git(repo, *args):
            result = subprocess.check_output(
                ["git", "-C", str(repo), *args], env=dict(os.environ, **environment)
            )
            return result.decode().strip()

        self.assertEqual(git(old, "rev-parse", "HEAD"), BASELINE)
        self.assertEqual(git(old, "status", "--porcelain"), "")
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, environment):
            root = Path(temp).resolve()
            source = root / "source"
            git(root, "clone", "--no-hardlinks", str(old), str(source))
            git(source, "remote", "set-url", "origin", formal.ORIGIN)
            git(source, "config", "user.name", "Synthetic Client tester")
            git(source, "config", "user.email", "fixture@example.invalid")
            origin = root / "channel.git"
            git(root, "init", "--bare", str(origin))

            groups = ("windows", "macos")
            old_tips, bridges, new_tips = {}, {}, {}
            with patch.object(formal, "ROOT", source):
                for group in groups:
                    output = root / ("old-" + group)
                    subprocess.check_call([
                        sys.executable, "-I", "-B", str(old / "dev/build/package.py"),
                        "--mode", "personal-content-https", "--version", "0.8.24",
                        "--platform", formal.GROUPS[group]["platforms"][0],
                        "--output", str(output),
                    ], stdout=subprocess.DEVNULL)
                    payload = {
                        p.relative_to(output).as_posix(): p.read_bytes()
                        for p in output.rglob("*") if p.is_file()
                    }
                    old_tips[group] = formal.runtime_commit(payload, (), BASELINE, "0.8.24")

                for member in git(old, "ls-files").splitlines():
                    shutil.copyfile(ROOT / member, source / member)
                git(source, "add", "--all")
                git(source, "commit", "-m", "Synthetic manifest bridge source")
                bridge_source = git(source, "rev-parse", "HEAD")
                for group, count in zip(groups, (185, 186)):
                    payload, _ = formal.source_payload(bridge_source)
                    archives, bridges[group], _ = formal.runtime_artifacts(
                        payload, (old_tips[group],), bridge_source, "0.8.25", group
                    )
                    self.assertEqual(len(archives), 2)
                    manifest = json.loads(git(
                        source, "show", bridges[group] + ":releases/manifest.json"
                    ))
                    self.assertEqual(len(manifest["files"]), count)

                (source / NEW).write_bytes(b"VALUE = 42\n")
                path = source / ENTRY
                raw = path.read_bytes()
                header = b"def self_check():\n"
                self.assertEqual(raw.count(header), 1)
                path.write_bytes(raw.replace(
                    header, header
                    + b"    from agent_brain_client.application.memory.bridge_fixture import VALUE\n"
                    + b"    assert VALUE == 42\n",
                ))
                git(source, "add", "--", NEW, ENTRY)
                git(source, "commit", "-m", "Synthetic optional Python member")
                new_source = git(source, "rev-parse", "HEAD")
                payload, _ = formal.source_payload(new_source)
                for group in groups:
                    with self.assertRaisesRegex(ValueError, "manifest-layout bridge"):
                        formal.runtime_artifacts(
                            payload, (old_tips[group],), new_source, "0.8.26", group
                        )
                    _, new_tips[group], _ = formal.runtime_artifacts(
                        payload, (bridges[group],), new_source, "0.8.26", group
                    )
                    before = json.loads(git(
                        source, "show", bridges[group] + ":releases/manifest.json"
                    ))
                    after = json.loads(git(
                        source, "show", new_tips[group] + ":releases/manifest.json"
                    ))
                    self.assertEqual(after["files"].keys() - before["files"].keys(), {NEW})
                    self.assertFalse(before["files"].keys() - after["files"].keys())

            def tip(commit):
                git(source, "push", str(origin), commit + ":" + REF)

            tip(old_tips["macos"])
            devices = [self.device(origin, old_tips["macos"], git) for _ in range(2)]
            first, skipped = devices
            tip(bridges["macos"])
            checked = self.official(old, first, "check-update")
            self.assertEqual(checked["status"], "client-update-available", checked)
            updated = self.official(old, first, "update")
            self.assertEqual(updated["active_package_commit"], bridges["macos"], updated)
            self.assertEqual(
                self.launcher(first, "--bootstrap-check")["active_package_commit"],
                bridges["macos"],
            )
            self.assertEqual(self.registration(first)["package"], first["channel"].verified_active()["package"])

            tip(new_tips["macos"])
            refusal = self.launcher(skipped, "--check-only", expected=2)
            self.assertEqual(refusal["status"], "release-review-required", refusal)
            self.assertEqual(refusal["active"]["commit"], old_tips["macos"])
            self.assertEqual(self.registration(skipped), skipped["registered"])

            # Only the first device has a bridge launcher. Invoke that registered
            # executable, then let the ordinary update path refresh its bindings.
            stepped = self.launcher(first, "--check-only")
            self.assertEqual(stepped["active"]["commit"], new_tips["macos"], stepped)
            for device in devices:
                available = self.official(ROOT, device, "check-update")
                self.assertTrue(available["channel_checked"], available)
                current = self.official(ROOT, device, "update")
                self.assertIn(current["status"], ("client-update-current", "client-update-updated"), current)
                self.assertEqual(current["active_package_commit"], new_tips["macos"])
                self.assertEqual(self.launcher(device, "--bootstrap-check")["active_package_commit"],
                                 new_tips["macos"])
                self.assertEqual(device["policy"].read_bytes(), device["before_policy"])
                self.assert_frozen(device)

    def device(self, origin, anchor, git):
        auth = HTTPSContent("test_exact_grant_mismatch_does_not_confirm_or_discard_prepare_recovery")
        auth.setUp()
        self.addCleanup(auth.doCleanups)
        _, seed, _, config = auth.install_fixture()
        path = Path(seed["release_policy"])
        value = json.loads(path.read_bytes())
        git(auth.base, "clone", "--no-hardlinks", str(origin), value["repository"])
        value.update(origin=str(origin), ref=REF, anchor_commit=anchor)
        metadata.write(path, value)
        pin = metadata.digest(path.read_bytes())
        channel = ClientReleases(path, pin)
        result = channel.update()
        self.assertEqual(result["status"], "updated", result)
        active = channel.verified_active()
        package = Path(active["package"])
        plan = installation.prepare(auth.home, package, path, pin, harness="codex")
        receipt = installation.apply(
            plan, metadata.digest(installation.contract.encode(plan)),
            check=lambda proposal: launcher_update.probe_launcher(proposal, active),
        )
        self.assertTrue(receipt["installed"], receipt)

        file = collection.settings_path(auth.home)
        private_directory(file.parent)
        pins = runtime_pins(package, manifest_sha256=active["manifest_sha256"])
        state = {
            "schema": 1, "enabled": False, "device_home": str(auth.home),
            "scope": {"schema": 1, "profile_id": "a" * 32, "include_roots": [],
                      "exclude_roots": [], "non_project_roots": [], "recall_personal": True},
            "approved_scope_sha256": None, "server_scope_sha256": None,
            "legacy_projects": [], "operator": None, "operator_sha256": None,
            "code_sha256": pins,
        }
        metadata.write(file, state)
        frozen = freeze_code(file.parent, pins, source=package)
        hooks.transact(config.parent, file.parent, hooks.block(state, root=frozen))
        buddy.install(auth.home, package, None, surface="both")
        device = {"home": auth.home, "config": config, "channel": channel,
                  "policy": path, "before_policy": path.read_bytes(), "state": state}
        device["registered"] = self.registration(device)
        return device

    def registration(self, device):
        file = device["home"] / ".config/agent-brain-client/codex-mcp.json"
        return metadata.private_json(file)

    def run_json(self, device, command, expected=0):
        env = dict(os.environ, HOME=str(device["home"]),
                   CODEX_HOME=str(device["config"].parent))
        result = subprocess.run(command, env=env, capture_output=True, timeout=180)
        self.assertEqual(result.returncode, expected, result.stdout.decode())
        return json.loads(result.stdout)

    def launcher(self, device, option, expected=0):
        _, command, _, _ = installation.registered_context(
            device["home"], self.registration(device)
        )
        return self.run_json(device, [command["command"], *command["args"], option], expected)

    def official(self, source, device, action):
        return self.run_json(device, [
            sys.executable, "-I", "-B", "-c", OFFICIAL_UPDATE, str(source), action
        ])

    def assert_frozen(self, device):
        file = collection.settings_path(device["home"])
        state = metadata.private_json(file)
        pins = state["code_sha256"]
        self.assertIn(NEW, pins)
        self.assertEqual(
            {k: v for k, v in state.items() if k != "code_sha256"},
            {k: v for k, v in device["state"].items() if k != "code_sha256"},
        )
        code = metadata.digest(json.dumps(pins, sort_keys=True).encode())
        codex_root = file.parent / "code" / code
        hooks_config = installation.tomllib.loads(device["config"].read_text())
        command = hooks_config["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn(str(codex_root), command)
        self.assertIn("code.json", command)
        roots = {"codex": codex_root, "codebuddy": buddy.state_directory(device["home"]) / "code" / code}
        for host, root in roots.items():
            self.assertEqual(metadata.private_json(root / "code.json")["files"], pins)
            self.assertEqual(metadata.digest((root / NEW).read_bytes()), pins[NEW])
            subprocess.check_call(
                [sys.executable, "-I", "-B", "-c", FROZEN_IMPORT, str(root), host],
                timeout=30,
            )
        receipt = metadata.private_json(
            buddy.state_directory(device["home"]) / "registration.json"
        )
        buddy_command = receipt["hooks"]["SessionStart"]["hooks"][0]["command"]
        self.assertIn(str(roots["codebuddy"]), buddy_command)
        env = dict(os.environ, HOME=str(device["home"]),
                   CODEX_HOME=str(device["config"].parent))
        for native_command in (command, buddy_command):
            # Paused sources exercise the registered Hook without contacting Server.
            ran = subprocess.run(
                shlex.split(native_command), input=b"{}", capture_output=True,
                timeout=30, check=True, env=env,
            )
            self.assertEqual(json.loads(ran.stdout), {})
