"""Start the registered command over real isolated, verified release snapshots."""

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest

from agent_brain_client.adapters.installation import launcher_update as hook_launcher
from agent_brain_client.adapters.harnesses.codex import collection_installation as codex
from agent_brain_client.adapters.harnesses.codebuddy.configuration import render_json
from agent_brain_client.adapters.packages.build import export
from agent_brain_client.adapters.packages.client import (
    ClientReleases, HTTPS_CONTENT_CAPABILITIES, https_files,
)
from agent_brain_client.adapters.packages.releases import digest
from agent_brain_client.adapters.platforms.filesystem import native_platform, private_directory
from agent_brain_client.adapters.storage import metadata


ROOT = Path(__file__).resolve().parents[1]
ENTRY = "src/agent_brain_client/runtime/host_binding.py"
EVENTS = {"SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit", "Stop": "stop"}


def host_fingerprints(config, file):
    """Codex 0.167.0-alpha.3.2 hook_hash/version_for_toml + persisted hook_key."""
    result = {}
    for name, label in EVENTS.items():
        for group_index, group in enumerate(config["hooks"][name]):
            for handler_index, handler in enumerate(group["hooks"]):
                command = (handler.get("commandWindows", handler["command"])
                           if os.name == "nt" else handler["command"])
                normalized = dict(type="command", command=command,
                                  timeout=handler.get("timeout", 600), **{"async": handler.get("async", False)})
                for field in ("statusMessage", "additionalContextLimit"):
                    if field in handler:
                        normalized[field] = handler[field]
                identity = {"event_name": label, "hooks": [normalized]}
                if "matcher" in group:
                    identity["matcher"] = group["matcher"]
                key = f"{file}:{label}:{group_index}:{handler_index}"
                raw = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
                result[key] = "sha256:" + hashlib.sha256(raw).hexdigest()
    return result


@unittest.skipUnless(sys.platform in ("darwin", "win32"), "Native Hook filesystem")
class StableHooks(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="synthetic-stable-hook-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = private_directory(self.root / "fictional home")
        self.native = private_directory(self.home / ".codex")
        self.state = private_directory(hook_launcher.native_hook_directory(self.home).parent)
        self.repository = private_directory(self.root / "repo")
        (self.repository / ".git").mkdir()
        platform = native_platform()
        profile, content = self.state / "profile.json", self.state / "content.json"
        profile_pin = metadata.write(profile, {})
        content_pin = metadata.write(content, {})
        bundle = self.root / "bundle"
        record = export(ROOT, bundle, https_content_candidate=True, target_platform=platform)
        self.files = record["files"]
        self.bundle = bundle
        credential = ("windows/native.py" if os.name == "nt" else "macos/native/CredentialStore.swift")
        policy = {
            "schema": 1, "adapter": "codex", "platform": platform, "origin": str(self.repository),
            "ref": "refs/heads/synthetic", "anchor_commit": "1" * 40, "channel": "stable",
            "core_api": 1, "state_schema": 1, "capabilities": HTTPS_CONTENT_CAPABILITIES,
            "file_prefixes": list(https_files(platform)), "repository": str(self.repository),
            "cache": str(self.root / "releases"), "runtime": {
                "device_home": str(self.home), "codex_home": str(self.native),
                "content_cache": str(self.state / "cache"), "https_profile": str(profile),
                "https_profile_sha256": profile_pin, "content_policy": str(content),
                "content_policy_sha256": content_pin,
                "credential_source_sha256": self.files["src/agent_brain_client/adapters/platforms/" + credential],
            },
        }
        policy_file = self.state / "policy.json"
        self.policy_pin = metadata.write(policy_file, policy)
        self.registration = self.state / "codex-mcp.json"
        metadata.write(self.registration, {"release_policy": str(policy_file), "release_policy_sha256": self.policy_pin})
        self.channel = ClientReleases(policy_file, self.policy_pin)
        self.config = self.native / "config.toml"
        self.hook_dir = private_directory(self.state / "global-memory")

    def release(self, version, commit, *, instrument=True):
        # Only dispatch is instrumented. The real release engine, manifest,
        # bootstrap, quoting and subprocess all execute in the synthetic home.
        source = (self.bundle / ENTRY).read_bytes()
        probe = ('\ndef run_native_hook(home, harness, settings=None):\n'
                 '    import json, sys\n'
                 f'    print(json.dumps(dict(version={version!r}, harness=harness, input=sys.stdin.read())))\n'
                 '    return 0\n').encode()
        edit = source + probe if instrument else source
        files = dict(self.files, **{ENTRY: digest(edit)})
        manifest = dict(schema=1, version=version, channel="stable", core_api=1, state_schema=1,
                        python_min="3.11", capabilities=HTTPS_CONTENT_CAPABILITIES, files=files, migrations=[])
        engine = self.channel.engine
        package = engine.prepare(commit, manifest, lambda name: edit if name == ENTRY else (self.bundle / name).read_bytes())
        pointer = dict(version=version, commit=commit, package=str(package),
                       manifest_sha256=digest(json.dumps(manifest, sort_keys=True).encode()))
        engine.set_active(pointer)
        self.assertEqual(self.channel.verified_active(serving=True), pointer)
        return pointer

    def run_hook(self, command):
        argv = command if os.name == "nt" else shlex.split(command)
        result = subprocess.run(argv, input="合成", encoding="utf-8", capture_output=True, check=True, timeout=45)
        return json.loads(result.stdout), result.stderr

    def test_post_migration_update_keeps_real_trust_keys_and_executes_active_release(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from agent_brain_client.adapters.storage.collection_setup import prepare

        first = self.release("0.8.27", "2" * 40)
        self.config.write_text('[features]\nhooks = true\n')
        prepare(SimpleNamespace(device_home=self.home, operator=None, operator_sha256=None), locate=None)
        codex.transact(self.native, self.hook_dir, codex.command_block("legacy-frozen-hook"))
        with self.config.open("a", encoding="utf-8") as file:
            file.write(
                '\n[[hooks.UserPromptSubmit]]\nmatcher = "synthetic"\n'
                '[[hooks.UserPromptSubmit.hooks]]\ntype = "command"\ncommand = "preserve-foreign"\n'
            )
        def fingerprint():
            return host_fingerprints(tomllib.loads(self.config.read_text()), self.config)

        def approve(rows):
            with self.config.open("a", encoding="utf-8") as file:
                for key, value in rows.items():
                    file.write(f'\n[hooks.state.{json.dumps(key)}]\ntrusted_hash = "{value}"\n')
        legacy = fingerprint()
        foreign = next(key for key in legacy if key.endswith(":user_prompt_submit:1:0"))
        approve({foreign: legacy[foreign]})
        command = hook_launcher.native_hook_command(self.home, "codex")
        self.assertNotIn(first["commit"], command)
        with patch.object(codex, "ROOT", Path(first["package"])):
            self.assertEqual(codex.refresh_hooks(self.home, check_only=True)["trust"],
                             "native-review-required-for-new-command")
            changed = codex.refresh_hooks(self.home)
        self.assertTrue(changed["native_changed"])
        self.assertEqual(changed["trust"], "native-review-required-for-new-command")
        migrated = fingerprint()
        self.assertEqual(migrated[foreign], legacy[foreign])
        self.assertEqual(migrated.keys(), legacy.keys())
        self.assertEqual(sum(migrated[k] != legacy[k] for k in migrated), 3)
        approve({key: value for key, value in migrated.items() if key != foreign})
        before = self.config.read_bytes()
        trusted = tomllib.loads(self.config.read_text())["hooks"]["state"]
        self.assertEqual({key: row["trusted_hash"] for key, row in trusted.items()}, migrated)
        self.assertEqual(self.run_hook(command)[0]["version"], "0.8.27")
        second = self.release("0.8.28", "3" * 40)
        with patch.object(codex, "ROOT", Path(second["package"])):
            unchanged = codex.refresh_hooks(self.home)
        self.assertFalse(unchanged["native_changed"])
        self.assertEqual(unchanged["trust"], "command-unchanged")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(fingerprint(), migrated)
        output, errors = self.run_hook(command)
        self.assertEqual(output, {"version": "0.8.28", "harness": "codex", "input": "合成"})
        self.assertEqual(errors, "")
        buddy = hook_launcher.native_hook_command(self.home, "codebuddy")
        self.assertEqual(self.run_hook(buddy)[0]["version"], "0.8.28")

        self.channel.engine.set_active(dict(second, package=str(self.root)))
        value, diagnostic = self.run_hook(command)
        self.assertEqual(value, {})
        self.assertIn("Hook launch unavailable", diagnostic)
        self.channel.engine.set_active(second)
        metadata.write(self.registration, dict(metadata.private_json(self.registration), release_policy_sha256="e" * 64))
        self.assertEqual(self.run_hook(command)[0], {})

    def test_host_bootstrap_rejects_mutable_code_before_execution(self):
        package = self.release("0.8.27", "2" * 40)
        hook_launcher.prepare_native_hook(self.home)
        root = hook_launcher.native_hook_directory(self.home)
        command = hook_launcher.native_hook_command(self.home, "codex")
        self.assertNotIn(package["commit"], command)
        expected = self.run_hook(command)
        self.assertEqual(expected[0]["version"], "0.8.27")

        marker = self.home / "synthetic-unexpected-execution"
        if os.name == "nt":
            rogue = self.home / "rogue.cmd"
            rogue.write_text(f'@echo ran>"{marker}"\n')
        else:
            rogue = self.home / "rogue"
            rogue.write_text(f"#!/bin/sh\nprintf ran > {shlex.quote(str(marker))}\n")
            rogue.chmod(0o700)
        (root / "python.txt").write_text(str(rogue) + "\n")
        self.assertEqual(self.run_hook(command), expected)
        self.assertFalse(marker.exists())

        script = root / "launcher.py"
        original = script.read_bytes()
        script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        self.assertEqual(self.run_hook(command)[0], {})
        self.assertFalse(marker.exists())
        script.write_bytes(original)
        receipt = root / "binding.json"
        binding = metadata.private_json(receipt)
        metadata.write(receipt, dict(binding, python=str(rogue)))
        self.assertEqual(self.run_hook(command)[0], {})
        self.assertFalse(marker.exists())

    def test_first_migration_recovers_exact_published_0826(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from agent_brain_client.adapters.packages.releases import atomic_json
        from agent_brain_client.adapters.storage.collection_setup import prepare

        commit = ("22b7522e93bac0812907192433016ea45c541d9a" if os.name == "nt"
                  else "be7957ec61b3b776ada0903f626a3c72d3d0972d")
        git = ["git", "-C", str(ROOT)]
        if subprocess.run([*git, "cat-file", "-e", commit], capture_output=True).returncode:
            self.skipTest("Fetch the official 0.8.26 platform release object to run the exact rollback")

        def read(revision, name):
            return subprocess.check_output([*git, "show", f"{revision}:{name}"])

        manifest = json.loads(read(commit, "releases/manifest.json"))
        prior = (commit, manifest, lambda name: read(commit, name))
        engine = self.channel.engine
        with patch.object(engine.source, "latest", return_value=prior):
            legacy = engine.check()["active"]
        self.assertEqual(legacy["version"], "0.8.26")
        self.assertNotIn(b"def run_native_hook(", (Path(legacy["package"]) / ENTRY).read_bytes())

        project = private_directory(self.home / "rollback-project")
        from agent_brain_client.adapters.storage.project_settings import project_settings
        from agent_brain_client.runtime.project_memory import refresh_project_hooks
        settings = project_settings(self.home, project, harness="codex")
        private_directory(settings.parent)
        old_entry = Path(legacy["package"]) / "src/agent_brain_client/interfaces/cli/project.py"
        old_cli = [sys.executable, "-I", "-B", str(old_entry)]
        created = subprocess.check_output([
            *old_cli, "setup", "--harness", "codex", "--device-home", str(self.home),
            "--project-root", str(project), "--output", str(settings),
        ])
        subprocess.check_output([
            *old_cli, "install", "--harness", "codex", "--settings", str(settings),
            "--settings-sha256", json.loads(created)["settings_sha256"],
        ])
        (settings.parent / "memory.off").write_text("paused")
        self.config.write_text('[features]\nhooks = true\n')
        prepare(SimpleNamespace(device_home=self.home, operator=None, operator_sha256=None), locate=None)
        codex.transact(self.native, self.hook_dir, codex.command_block("legacy-frozen-hook"))
        # This is a fresh verified candidate built from the current source.
        manifest = dict(manifest, version="0.8.27", files=self.files)
        nxt = ("2" * 40, manifest, lambda name: (self.bundle / name).read_bytes())
        with patch.object(engine.source, "latest", return_value=nxt):
            target = engine.check()["active"]
        with patch.object(codex, "ROOT", Path(target["package"])):
            self.assertTrue(codex.refresh_hooks(self.home)["native_changed"])
        self.assertEqual(refresh_project_hooks(self.home, "codex")["migrated"], 1)
        project_config = project / ".codex/hooks.json"
        project_before = project_config.read_bytes()
        project_command = hook_launcher.native_hook_command(self.home, "codex", settings)
        command = hook_launcher.native_hook_command(self.home, "codex")
        approved = host_fingerprints(tomllib.loads(self.config.read_text()), self.config)
        with self.config.open("a", encoding="utf-8") as file:
            for key, value in approved.items():
                file.write(f'\n[hooks.state.{json.dumps(key)}]\ntrusted_hash = "{value}"\n')
        native_bytes = self.config.read_bytes()
        # Replay native verified rollback from the real activation receipt's
        # predecessor after fixed commands have been written.
        state = json.loads(engine.journal.read_bytes())
        self.assertEqual((state["before"], state["target"]), (legacy, target))
        atomic_json(engine.journal, dict(state, phase="activated"))
        with patch.object(engine.source, "latest", return_value=prior):
            self.assertEqual(engine.check()["active"], legacy)
        self.assertEqual(json.loads(engine.journal.read_bytes())["phase"], "rolled-back-interrupted")
        self.assertEqual(self.channel.verified_active(serving=True), legacy)
        self.assertEqual(self.config.read_bytes(), native_bytes)
        _, diagnostics = self.run_hook(command)
        self.assertIn("serving verified 0.8.26 rollback", diagnostics)
        self.assertIn("global-hook-timing", diagnostics)
        _, buddy = self.run_hook(hook_launcher.native_hook_command(self.home, "codebuddy"))
        self.assertIn("serving verified 0.8.26 rollback", buddy)
        self.assertIn('"operation": "codebuddy-hook"', buddy)
        self.assertEqual(project_config.read_bytes(), project_before)
        response, project_diagnostic = self.run_hook(project_command)
        self.assertEqual(response, {})
        self.assertEqual(project_diagnostic.strip(), "agent-brain Hook serving verified 0.8.26 rollback")

        with patch.object(engine.source, "latest", return_value=nxt):
            self.assertEqual(engine.check()["active"], target)
        with patch.object(codex, "ROOT", Path(target["package"])):
            self.assertFalse(codex.refresh_hooks(self.home)["native_changed"])
        self.assertEqual(self.config.read_bytes(), native_bytes)
        self.assertIn("global-hook-timing", self.run_hook(command)[1])
        self.assertFalse(refresh_project_hooks(self.home, "codex")["native_changed"])
        self.assertEqual(project_config.read_bytes(), project_before)
        self.assertEqual(self.run_hook(project_command), ({}, ""))

    def test_project_migration_keeps_stopped_scope_and_checks_the_receipt_pin(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from agent_brain_client.adapters.harnesses.codebuddy import project_configuration as buddy_project
        from agent_brain_client.adapters.harnesses.codex import project_configuration as codex_project
        from agent_brain_client.adapters.storage.project_settings import project_settings
        from agent_brain_client.runtime import project_memory

        self.release("0.8.27", "2" * 40, instrument=False)
        project = private_directory(self.home / "fictional project")
        for harness, adapter in (("codex", codex_project), ("codebuddy", buddy_project)):
            path = project_settings(self.home, project, harness=harness)
            prepared = project_memory.setup(SimpleNamespace(
                device_home=self.home, project_root=project, harness=harness,
                output=path, server_bundle=None, query="synthetic",
            ))
            settings = project_memory.load(path, prepared["settings_sha256"])
            # Future packages own executable integrity. This deliberately old
            # code pin remains present in the original project settings.
            settings["code_sha256"][ENTRY] = "f" * 64
            pin = metadata.write(path, settings)
            marker = Path(settings["off_file"])
            marker.write_text("paused")
            args = (settings, path, pin) if harness == "codex" else (settings, path)
            legacy = copy.deepcopy(adapter.definitions(*args))
            for group in legacy.values():
                group["hooks"][0]["command"] = "synthetic-legacy-project-hook"
            with patch.object(adapter, "definitions", return_value=legacy):
                adapter.install(settings, path, pin)
            before = path.read_bytes()
            check = project_memory.refresh_project_hooks(self.home, harness, check_only=True)
            self.assertTrue(check["update_available"], check)
            result = project_memory.refresh_project_hooks(self.home, harness)
            self.assertEqual(result["migrated"], 1, result)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(marker.read_text(), "paused")
            output, diagnostic = self.run_hook(hook_launcher.native_hook_command(self.home, harness, path))
            self.assertEqual(output, {})
            self.assertEqual(diagnostic, "")
            self.assertFalse(project_memory.refresh_project_hooks(self.home, harness)["native_changed"])

            # The stable command must never recalculate trust from modified settings.
            metadata.write(path, dict(settings, query="unapproved"))
            output, diagnostic = self.run_hook(hook_launcher.native_hook_command(self.home, harness, path))
            self.assertEqual(output, {})
            self.assertIn("review-required", diagnostic)

    def test_codebuddy_keeps_unrelated_settings_bytes_on_noop(self):
        before = '{// preserve comment\n"permissions":{"deny":["synthetic"]},"hooks":{"Stop":[]}}\n'
        definitions = {event: {"hooks": [{"type": "command", "command": hook_launcher.native_hook_command(self.home, "codebuddy")}]}
                       for event in EVENTS}
        installed = render_json(before, definitions, None, hooks=True)
        self.assertEqual(render_json(installed.decode(), definitions, definitions, hooks=True), installed)
        self.assertIn(b'"permissions":{"deny":["synthetic"]}', installed)


if __name__ == "__main__":
    unittest.main()
