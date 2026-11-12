import agent_brain_client.interfaces.cli.collection as collection_cli
import agent_brain_client.adapters.storage.collection as source_policy
import agent_brain_client.adapters.storage.metadata as metadata
"""Both native host writers, fictional metadata, no server or credential IO."""

import copy
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from agent_brain_client.adapters.platforms.filesystem import private_directory
import agent_brain_client.adapters.harnesses.codex.configuration as host
import agent_brain_client.adapters.storage.host_files as host_files
import agent_brain_client.runtime.collection as gm
import agent_brain_client.adapters.harnesses.codex.collection_installation as hooks
import agent_brain_client.adapters.harnesses.codex.project_configuration as project_sources
from agent_brain_client.adapters.harnesses.codex.updates import CodexUpdateInstallation


class GlobalMemoryUpdate(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="synthetic-hook-update-")
        self.addCleanup(temp.cleanup)
        self.home = private_directory(Path(temp.name).resolve() / "fixture")
        self.native = private_directory(self.home / "codex")
        self.file = source_policy.settings_path(self.home)
        private_directory(self.file.parent)
        state = self.file.parent.parent
        for name in ("profile.json", "content.json"):
            metadata.write(state / name, {"schema": 1})
        policy = {
            "capabilities": [
                "codex-stdio-mcp",
                "personal-content-explicit",
                "https-system-credential",
            ],
            "runtime": {
                "codex_home": str(self.native),
                "https_profile": str(state / "profile.json"),
                "https_profile_sha256": metadata.digest(
                    (state / "profile.json").read_bytes()
                ),
                "content_policy": str(state / "content.json"),
                "content_policy_sha256": metadata.digest(
                    (state / "content.json").read_bytes()
                ),
            },
        }
        metadata.write(state / "policy.json", policy)
        metadata.write(
            state / "codex-mcp.json",
            {
                "release_policy": str(state / "policy.json"),
                "release_policy_sha256": metadata.digest(
                    (state / "policy.json").read_bytes()
                ),
            },
        )
        self.value = {
            "schema": 1,
            "enabled": False,
            "device_home": str(self.home),
            "scope": {
                "schema": 1,
                "profile_id": "a" * 32,
                "include_roots": [],
                "exclude_roots": [],
                "non_project_roots": [],
                "recall_personal": True,
            },
            "approved_scope_sha256": None,
            "server_scope_sha256": None,
            "legacy_projects": [],
            "operator": None,
            "operator_sha256": None,
            "code_sha256": {name: "b" * 64 for name in hooks.FILES},
        }
        metadata.write(self.file, self.value)
        self.config = self.native / "config.toml"
        metadata.write(self.config, {})
        self.config.write_bytes(b'# User setting\nmodel = "synthetic"\n')
        hooks.transact(self.native, self.file.parent, hooks.block(self.value))
        self.no_client = patch.object(
            gm, "CollectionClient", side_effect=AssertionError("No credential activation")
        )
        self.no_client.start()
        self.addCleanup(self.no_client.stop)




    def old_installer(self):
        root = self.home / "synthetic-old-installer"
        for name in hooks.FILES:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(hooks.ROOT / name, target)
        source = root / "src/agent_brain_client/adapters/harnesses/codex/collection.py"
        source.write_bytes(source.read_bytes() + b"\n# Synthetic prior Hook source.\n")
        with patch.object(hooks, "ROOT", root):
            hooks.refresh(self.home)
            self.assertEqual(hooks.refresh(self.home)["status"], "global-hook-current")
        return root

    def update_installation(self):
        active = {"version": "0.8.4", "commit": "c" * 40, "package": str(hooks.ROOT)}
        installation = CodexUpdateInstallation(self.home, setup_sources=lambda: None)
        installation.channel = SimpleNamespace(verified_active=lambda: active)
        return installation

    def test_update_refresh_uses_verified_package_not_old_installer(self):
        installation = self.update_installation()
        old = self.old_installer()
        before_settings = metadata.private_json(self.file)
        pending = self.file.parent / "synthetic-pending.json"
        metadata.write(pending, {"state": "pending", "request_id": "synthetic-old"})
        off = self.file.parent / "global.off"
        off.write_bytes(b"Synthetic paused capture")
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        with patch.object(hooks, "ROOT", old):
            checked = installation.refresh_hooks(check_only=True)
            self.assertTrue(checked["update_available"], checked)
            self.assertEqual(before, {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()})
            updated = installation.refresh_hooks()
            self.assertEqual(updated["status"], "global-hook-updated", updated)
            self.assertEqual(installation.refresh_hooks()["status"], "global-hook-current")
        after = source_policy.load(self.home)
        self.assertNotEqual(after["code_sha256"], before_settings["code_sha256"])
        self.assertEqual({k: v for k, v in after.items() if k != "code_sha256"},
                         {k: v for k, v in before_settings.items() if k != "code_sha256"})
        self.assertEqual(pending.read_bytes(), before[pending])
        self.assertEqual(off.read_bytes(), before[off])
        self.assertTrue(hooks.installation_status(after))

    def test_update_refresh_uses_target_renderer_and_reports_its_conflicts(self):
        installation = self.update_installation()
        target = self.old_installer()
        renderer = target / "src/agent_brain_client/adapters/harnesses/codex/collection_installation.py"
        renderer.write_text(renderer.read_text().replace(
            '"timeout = " + "120"',
            '"timeout = " + "121"',
        ))
        active = {"version": "0.8.4", "commit": "d" * 40, "package": str(target)}
        installation.channel = SimpleNamespace(verified_active=lambda: active)
        result = installation.refresh_hooks()
        self.assertEqual(result["source_package_commit"], active["commit"])
        self.assertIn("timeout = 121", self.config.read_text())
        self.assertEqual(installation.refresh_hooks()["status"], "global-hook-current")
        self.config.write_bytes(self.config.read_bytes().replace(b"timeout = 121", b"timeout = 119"))
        before = self.config.read_bytes()
        from agent_brain_client.application.installation.update_ports import ManagedConfigurationConflict
        with self.assertRaises(ManagedConfigurationConflict) as error:
            installation.refresh_hooks()
        self.assertEqual(error.exception.field, "config.hooks.SessionStart[0].hooks[0].timeout")
        self.assertEqual(self.config.read_bytes(), before)

    def test_codebuddy_refresh_uses_active_package_renderer_and_pins(self):
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        from agent_brain_client.runtime.updates import refresh_bindings

        configuration.install(self.home, hooks.ROOT, None, surface="both")
        target = self.old_installer()
        renderer = target / "src/agent_brain_client/adapters/harnesses/codebuddy/configuration.py"
        renderer.write_text(renderer.read_text().replace('"timeout": 90', '"timeout": 91'))
        active = {"version": "0.8.5", "commit": "e" * 40, "package": str(target)}
        installation = CodexUpdateInstallation(
            self.home, setup_sources=lambda: None,
            refresh_bindings=lambda package, **kw: refresh_bindings(self.home, package, **kw),
        )
        installation.channel = SimpleNamespace(verified_active=lambda: active)
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        with patch.object(configuration, "refresh", side_effect=AssertionError("No old renderer")):
            checked = installation.refresh_hooks(check_only=True)
            self.assertTrue(checked["codebuddy"]["update_available"])
            self.assertEqual(before, {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()})
            result = installation.refresh_hooks()
        self.assertTrue(result["codebuddy"]["native_changed"])
        receipt = metadata.private_json(configuration.state_directory(self.home) / "registration.json")
        self.assertEqual(receipt["hooks"]["Stop"]["hooks"][0]["timeout"], 91)
        frozen = Path(receipt["mcp"]["args"][4]).parents[4]
        frozen_pins = metadata.private_json(frozen / "code.json")["files"]
        self.assertEqual(frozen_pins, metadata.private_json(self.file)["code_sha256"])
        self.assertEqual((frozen / renderer.relative_to(target)).read_bytes(), renderer.read_bytes())
        self.assertEqual(result["source_package_commit"], active["commit"])

    def test_old_active_package_without_binding_refresh_does_not_block_discovery(self):
        from agent_brain_client.runtime.updates import refresh_bindings

        target = self.old_installer()
        renderer = target / "src/agent_brain_client/adapters/harnesses/codebuddy/configuration.py"
        renderer.write_text(renderer.read_text().replace("def refresh(", "def future_refresh("))
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        result = refresh_bindings(self.home, target, check_only=True)
        self.assertEqual(result["status"], "codebuddy-refresh-requires-new-package")
        self.assertFalse(result["native_changed"])
        self.assertEqual(before, {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()})

    def test_update_refresh_identical_package_avoids_subprocess(self):
        installation = self.update_installation()
        hooks.refresh(self.home)
        with patch.object(subprocess, "run", side_effect=AssertionError("No child needed")):
            result = installation.refresh_hooks(check_only=True)
        self.assertEqual(result["status"], "global-hook-current")
        self.assertEqual(result["source_package_version"], "0.8.4")
        self.assertEqual(result["source_package_commit"], "c" * 40)

    def test_update_refresh_translates_target_host_edit_in_progress(self):
        from agent_brain_client.application.installation.update_ports import HostEditInProgress
        installation = self.update_installation()
        old = self.old_installer()
        before = self.config.read_bytes()
        response = subprocess.CompletedProcess([], 0, b'{"host_edit_in_progress": true}', b"")
        with patch.object(hooks, "ROOT", old), patch.object(subprocess, "run", return_value=response):
            with self.assertRaises(HostEditInProgress):
                installation.refresh_hooks()
        self.assertEqual(self.config.read_bytes(), before)

    def test_update_refresh_rejects_unverified_package_without_local_fallback(self):
        installation = self.update_installation()
        installation.channel = SimpleNamespace(verified_active=lambda: None)
        before = self.config.read_bytes()
        with patch.object(hooks, "refresh", side_effect=AssertionError("No stale fallback")):
            with self.assertRaises(ValueError):
                installation.refresh_hooks()
        self.assertEqual(self.config.read_bytes(), before)

    def test_update_refresh_rejects_failed_child_and_changed_active_pointer(self):
        from unittest.mock import Mock
        installation = self.update_installation()
        active = installation.channel.verified_active()
        old = self.old_installer()
        before = self.config.read_bytes()
        with patch.object(hooks, "ROOT", old):
            for response in (subprocess.CompletedProcess([], 2, b"", b"synthetic"),
                             subprocess.CompletedProcess([], 0, b'{"result": {}}', b"")):
                with self.subTest(response=response), patch.object(subprocess, "run", return_value=response):
                    with self.assertRaises(ValueError):
                        installation.refresh_hooks()
            self.assertEqual(self.config.read_bytes(), before)
            changed = dict(active, commit="e" * 40)
            installation.channel.verified_active = Mock(side_effect=[active, changed])
            with self.assertRaises(ValueError):
                installation.refresh_hooks(check_only=True)
            self.assertEqual(self.config.read_bytes(), before)

    def test_refresh_migrates_owned_async_stop_without_removing_pending_data(self):
        previous = hooks.block(self.value).replace('async = false', 'async = true')
        hooks.transact(self.native, self.file.parent, previous)
        pending = self.file.parent / 'fictional-pending.json'
        metadata.write(pending, {'request_id': 'fictional-retained-request'})
        before = pending.read_bytes()
        hooks.refresh(self.home)
        stop = hooks.tomllib.loads(self.config.read_text())['hooks']['Stop'][0]['hooks'][0]
        self.assertIs(stop['async'], False)
        self.assertEqual(pending.read_bytes(), before)

    def preserved(self):
        actual = metadata.private_json(self.file)
        self.assertEqual(
            {k: v for k, v in actual.items() if k != "code_sha256"},
            {k: v for k, v in self.value.items() if k != "code_sha256"},
        )
        self.assertIn(b'# User setting\nmodel = "synthetic"', self.config.read_bytes())
        return actual

    def test_check_is_read_only_then_refresh_loads_frozen_runtime_and_is_idempotent(
        self,
    ):
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        self.assertTrue(hooks.refresh(self.home, check_only=True)["update_available"])
        self.assertEqual(
            before, {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        )
        result = hooks.refresh(self.home)
        self.assertEqual(result["status"], "global-hook-updated")
        self.assertTrue(result["native_changed"])
        self.preserved()
        self.assertTrue(hooks.installation_status(source_policy.load(self.home)))
        root = next((self.file.parent / "code").iterdir())
        code = 'import sys;sys.path.insert(0,sys.argv[1]);import agent_brain_client.runtime.collection;import agent_brain_client.interfaces.mcp.content;from agent_brain_client.adapters.storage.collection import load;load(sys.argv[2]);print("frozen-runtime-loaded")'
        environment = dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.native))
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                code,
                str(root / "src"),
                str(self.home),
            ],
            cwd=self.home,
            env=environment,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn(b"frozen-runtime-loaded", proc.stdout)
        self.assertEqual(hooks.refresh(self.home)["status"], "global-hook-current")

    def test_approved_paused_sources_routes_operator_and_off_switch_are_preserved(self):
        self.value.update(
            enabled=True,
            approved_scope_sha256="c" * 64,
            server_scope_sha256="d" * 64,
            legacy_projects=[
                {
                    "root": str(self.home / "fictional-project"),
                    "capture_delegated": False,
                }
            ],
            operator=str(self.home / "operator.json"),
            operator_sha256="e" * 64,
        )
        metadata.write(self.file, self.value)
        off = self.file.parent / "global.off"
        off.write_bytes(b"Paused by user")
        hooks.refresh(self.home)
        self.preserved()
        self.assertEqual(off.read_bytes(), b"Paused by user")
        self.assertFalse(source_policy.approved(source_policy.load(self.home)))

    def test_absent_and_explicitly_uninstalled_hooks_are_never_added(self):
        hooks.transact(self.native, self.file.parent, "", remove=True)
        before = self.config.read_bytes()
        self.assertEqual(
            hooks.refresh(self.home)["status"], "global-hook-not-installed"
        )
        self.assertEqual(self.config.read_bytes(), before)
        self.file.unlink()
        self.assertEqual(
            hooks.refresh(self.home)["status"], "global-hook-not-installed"
        )

    def test_install_and_uninstall_preserve_existing_empty_hooks_table(self):
        hooks.transact(self.native, self.file.parent, '', remove=True)
        self.config.write_bytes(self.config.read_bytes() + b'\n[hooks]\n')
        hooks.transact(self.native, self.file.parent, hooks.block(self.value))
        hooks.transact(self.native, self.file.parent, '', remove=True)
        self.assertIn(b'[hooks]', self.config.read_bytes())
        self.assertEqual(hooks.tomllib.loads(self.config.read_text())['hooks'], {})

    def test_foreign_hook_edit_is_preserved(self):
        self.config.write_bytes(
            self.config.read_bytes().replace(b"timeout = 120", b"timeout = 119")
        )
        before = self.config.read_bytes()
        with self.assertRaises(ValueError):
            hooks.refresh(self.home)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(metadata.private_json(self.file), self.value)

    def test_host_trust_tables_survive_refresh_without_becoming_owned(self):
        state = (b'[hooks.state."synthetic:session_start:0:0"]\ntrusted_hash = "sha256:one"\n'
                 b'[hooks.state."synthetic:stop:0:0"]\nenabled = false\ntrusted_hash = "sha256:two"\n')
        before = (self.config.read_bytes().replace(b'timeout = 120', b'timeout=120')
                  .replace(hooks.END.encode(), state + hooks.END.encode()))
        self.config.write_bytes(before)
        self.assertTrue(hooks.installation_status(self.value))
        self.assertTrue(hooks.refresh(self.home, check_only=True)['update_available'])
        self.assertEqual(self.config.read_bytes(), before)
        result = hooks.refresh(self.home)
        self.assertEqual(result['status'], 'global-hook-updated')
        self.assertIn(state, self.config.read_bytes())
        self.assertTrue(hooks.installation_status(source_policy.load(self.home)))
        before_current = self.config.read_bytes()
        self.assertEqual(hooks.refresh(self.home)['status'], 'global-hook-current')
        self.assertEqual(self.config.read_bytes(), before_current)
        self.preserved()
        hooks.transact(self.native, self.file.parent, '', remove=True)
        self.assertIn(state, self.config.read_bytes())
        self.assertNotIn(hooks.BEGIN.encode(), self.config.read_bytes())

    def test_trust_table_does_not_hide_changed_hook_definition(self):
        state = b'[hooks.state."synthetic:stop:0:0"]\ntrusted_hash = "sha256:one"\n'
        before = self.config.read_bytes().replace(b'timeout = 120', b'timeout = 119').replace(hooks.END.encode(), state + hooks.END.encode())
        self.config.write_bytes(before)
        with self.assertRaises(ValueError):
            hooks.refresh(self.home)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(hooks.same_owned('[[hooks.Stop]]\nasync=true\n', '[[hooks.Stop]]\nasync=1\n'))

    def test_third_party_tables_inside_markers_survive_check_refresh_and_removal(self):
        foreign = (b'# Synthetic third-party configuration\n'
                   b'[mcp_servers.node_repl]\ncommand = "synthetic-node"\n'
                   b'args = ["--synthetic"]\n'
                   b'[mcp_servers.node_repl.env]\nSYNTHETIC = "keep"\n'
                   b'[apps."synthetic-sites"]\nenabled = true\n'
                   b'[hooks.state."synthetic:stop:0:0"]\n'
                   b'enabled = false\ntrusted_hash = "sha256:keep"\n')
        before = self.config.read_bytes().replace(b'[[hooks.UserPromptSubmit]]', foreign + b'[[hooks.UserPromptSubmit]]')
        self.config.write_bytes(before)
        expected = hooks.unowned_config(hooks.tomllib.loads(before.decode()), hooks.block(self.value))
        self.assertTrue(hooks.installation_status(self.value))
        self.assertTrue(hooks.refresh(self.home, check_only=True)['update_available'])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(metadata.private_json(self.file), self.value)
        hooks.refresh(self.home)
        self.assertIn(foreign, self.config.read_bytes())
        current = source_policy.load(self.home)
        self.assertTrue(hooks.installation_status(current))
        self.assertEqual(hooks.unowned_config(hooks.tomllib.loads(self.config.read_text()),
                                             hooks.split_owned(self.config.read_bytes())[1]), expected)
        updated = self.config.read_bytes()
        self.assertEqual(hooks.refresh(self.home)['status'], 'global-hook-current')
        self.assertEqual(self.config.read_bytes(), updated)
        hooks.transact(self.native, self.file.parent, '', remove=True)
        self.assertIn(foreign, self.config.read_bytes())
        self.assertEqual(hooks.tomllib.loads(self.config.read_text()), expected)

    def test_foreign_tables_do_not_hide_owned_hook_edits(self):
        foreign = b'[mcp_servers.node_repl]\ncommand = "synthetic-node"\n'
        before = self.config.read_bytes().replace(b'[[hooks.Stop]]', foreign + b'[[hooks.Stop]]').replace(b'async = false', b'async = true')
        self.config.write_bytes(before)
        for action in (lambda: hooks.refresh(self.home, check_only=True),
                       lambda: hooks.refresh(self.home),
                       lambda: hooks.transact(self.native, self.file.parent, '', remove=True)):
            with self.assertRaisesRegex(ValueError, 'Outside edit'):
                action()
            self.assertEqual(self.config.read_bytes(), before)
            self.assertEqual(metadata.private_json(self.file), self.value)

    def test_multiline_foreign_data_and_quoted_headers_preserve_toml_scope(self):
        foreign = (b'["apps"."synthetic-sites"]\ntext = """\n'
                   b'[hooks.Stop]\nexample only\n"""\n'
                   b"['hooks'.'state'.'synthetic:stop:0:0']\n"
                   b'enabled = false\ntrusted_hash = "sha256:keep"\n')
        before = self.config.read_bytes().replace(hooks.END.encode(), foreign + hooks.END.encode()).replace(b'\n', b'\r\n')
        self.config.write_bytes(before)
        parsed = hooks.tomllib.loads(before.decode())
        self.assertTrue(hooks.installation_status(self.value))
        hooks.refresh(self.home)
        after = hooks.tomllib.loads(self.config.read_text())
        self.assertEqual(after['apps'], parsed['apps'])
        self.assertEqual(after['hooks']['state'], parsed['hooks']['state'])
        self.assertIn(foreign.rstrip().replace(b'\n', b'\r\n'), self.config.read_bytes())

    def test_interrupted_hook_write_with_foreign_tables_restores_exact_preimage(self):
        foreign = b'[apps."synthetic-sites"]\nenabled = false\n'
        before = self.config.read_bytes().replace(hooks.END.encode(), foreign + hooks.END.encode())
        self.config.write_bytes(before)
        target = hooks.block(dict(self.value, code_sha256={name: 'c' * 64 for name in hooks.FILES}))
        apply = host_files.host_apply

        def interrupt(rows, persist):
            apply(rows, persist)
            raise SystemExit('Synthetic interruption after Hook write')

        with patch.object(host_files, 'host_apply', side_effect=interrupt), self.assertRaises(SystemExit):
            hooks.transact(self.native, self.file.parent, target)
        receipt = self.file.parent / 'hook-install.json'
        record = hooks.read_receipt(receipt)
        self.assertEqual(record['phase'], 'applying')
        hooks.recover_record(self.config, receipt, record)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(metadata.private_json(self.file), self.value)
        hooks.transact(self.native, self.file.parent, target)
        self.assertIn(foreign, self.config.read_bytes())

    def test_previous_version_journal_with_comments_before_trust_table_recovers(self):
        state = '[hooks.state."synthetic:stop:0:0"]\nenabled = false\ntrusted_hash = "sha256:keep"\n'
        comments = '\n# Host trust comment retained in the project_sources owned image\n'
        original = self.config.read_text()
        before = original.replace(hooks.END, comments + state + hooks.END).encode()
        self.config.write_bytes(before)
        # Schema-2 images produced by the previous splitter: only the state
        # table moved; comments/blank lines stayed inside previous_block.
        previous = hooks.block(self.value).replace(hooks.END, comments + hooks.END)
        outside = original[:original.index(hooks.BEGIN)] + state
        target = hooks.block(dict(self.value, code_sha256={name: 'c' * 64 for name in hooks.FILES}))
        after = (outside.rstrip() + '\n\n' + target).encode()
        receipt = self.file.parent / 'hook-install.json'
        record = {'schema': 2, 'phase': 'applying', 'block': target,
                  'previous_block': previous, 'before_sha256': metadata.digest(before),
                  'after_sha256': metadata.digest(after), 'host': host_files.host_plan({self.config: after})}
        host_files.host_apply(record['host'], lambda: metadata.write(receipt, record))
        hooks.recover_record(self.config, receipt, hooks.read_receipt(receipt))
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(hooks.read_receipt(receipt)['phase'], 'rolled-back')
        self.assertFalse(hooks.installation_status(self.value))
        hooks.transact(self.native, self.file.parent, target)
        self.assertEqual(hooks.tomllib.loads(self.config.read_text())['hooks']['state'],
                         hooks.tomllib.loads(before.decode())['hooks']['state'])

    def test_trailing_host_state_is_preserved_when_tables_move(self):
        state = b'[hooks.state."synthetic:stop:0:0"]\ntrusted_hash = "sha256:one"\n'
        before = self.config.read_bytes().replace(hooks.END.encode(), state + hooks.END.encode()) + b'custom_host_field = "keep"\n'
        self.config.write_bytes(before)
        original = hooks.tomllib.loads(before.decode())['hooks']['state']
        hooks.refresh(self.home)
        self.assertEqual(hooks.tomllib.loads(self.config.read_text())['hooks']['state'], original)
        self.assertIn(b'custom_host_field = "keep"', self.config.read_bytes())

    def test_metadata_status_reports_old_code_and_checks_server_without_changing_state(self):
        before = {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        self.no_client.stop()
        response = {'result': {'metadata_only': True, 'source_registered': False,
                               'source_device_id': 'd' * 32, 'global_profile_id': 'e' * 32}}
        client = SimpleNamespace(wire=lambda method, params: response)
        output = io.StringIO()
        with (patch.object(gm, 'CollectionClient', return_value=client),
              patch.object(sys, 'argv', ['global_memory', 'global-status', '--device-home', str(self.home), '--check-server']),
              patch.object(sys, 'stdout', output)):
            self.assertEqual(collection_cli.main(), 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result['checkout_code_matches_hook_pins'])
        self.assertIsNone(result['expanded_sources_enabled'])
        self.assertEqual(result['server_admission'], response)
        with self.assertRaises(ValueError):
            source_policy.load(self.home)
        self.assertEqual(before, {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()})

    def test_isolated_status_cli_loads_shared_toml_editor_without_pythonpath(self):
        environment = dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.native))
        proc = subprocess.run([sys.executable, '-I', '-B', str(gm.ROOT / 'src/agent_brain_client/interfaces/cli/collection.py'),
                               'global-status', '--device-home', str(self.home)],
                              cwd=self.home, env=environment, capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode() + proc.stderr.decode())
        self.assertTrue(json.loads(proc.stdout)['global_entry_installed'])

    def test_retained_removal_requires_authenticated_journal_and_exact_block(self):
        import agent_brain_client.adapters.harnesses.codex.lifecycle as lifecycle
        outside, previous = hooks.split_owned(self.config.read_bytes())
        lines = previous.splitlines(keepends=True)
        for index in range(len(lines) - 1):
            if lines[index].startswith('type = ') and lines[index + 1].startswith('command = '):
                lines[index], lines[index + 1] = lines[index + 1], lines[index]
                break
        reordered = ''.join(lines)
        self.assertNotEqual(reordered, previous)
        self.assertTrue(hooks.same_owned(reordered, previous))
        self.config.write_bytes(outside.encode())
        with self.assertRaises((ValueError, OSError)):
            hooks.refresh(self.home)
        for phase, block, accepted in (('applying', previous, False),
                                        ('complete', 'Changed block', False),
                                        ('complete', previous, True),
                                        ('reinstalled', previous, True),
                                        ('complete', reordered, True),
                                        ('reinstalled', reordered, True),
                                        ('complete', reordered.replace('timeout = 120', 'timeout = 119'), False)):
            with (patch.object(lifecycle, 'read_removal', return_value={'phase': phase, 'hook_block': block}),
                  patch.object(lifecycle, 'validate_journal') as authenticate):
                if accepted:
                    self.assertEqual(hooks.refresh(self.home)['status'], 'global-hook-not-installed')
                else:
                    with self.assertRaises(ValueError):
                        hooks.refresh(self.home)
                authenticate.assert_called_once()
        self.assertEqual(self.config.read_bytes(), outside.encode())
        self.assertEqual(metadata.private_json(self.file), self.value)

    def test_interrupted_settings_write_is_recovered_before_retry(self):
        apply = host_files.host_apply

        def interrupt(rows, persist):
            apply(rows, persist)
            if str(self.file) in rows:
                raise SystemExit("Synthetic interruption after settings write")

        with (
            patch.object(host_files, "host_apply", side_effect=interrupt),
            self.assertRaises(SystemExit),
        ):
            hooks.refresh(self.home)
        self.assertTrue(hooks.recover_update(self.home, check_only=True))
        self.assertEqual(
            hooks.read_receipt(self.file.parent / "code-refresh.json")["phase"],
            "applying",
        )
        self.assertTrue(hooks.recover_update(self.home))
        self.assertEqual(metadata.private_json(self.file), self.value)
        self.assertEqual(hooks.refresh(self.home)["status"], "global-hook-updated")
        self.preserved()

    def test_outside_settings_edit_after_interruption_is_preserved(self):
        apply = host_files.host_apply

        def interrupt(rows, persist):
            apply(rows, persist)
            if str(self.file) in rows:
                raise SystemExit("Synthetic interruption")

        with (
            patch.object(host_files, "host_apply", side_effect=interrupt),
            self.assertRaises(SystemExit),
        ):
            hooks.refresh(self.home)
        value = copy.deepcopy(metadata.private_json(self.file))
        value["enabled"] = True
        metadata.write(self.file, value)
        with self.assertRaises(ValueError):
            hooks.recover_update(self.home)
        self.assertEqual(metadata.private_json(self.file), value)

    def test_recognized_torn_settings_write_recovers_original_then_retries(self):
        stage = host_files.host_stage

        @contextmanager
        def inplace(path, raw, mode, expected):
            if path != self.file or os.name == "nt":
                with stage(path, raw, mode, expected) as commit:
                    yield commit
                return
            with path.open("r+b", buffering=0) as stream:
                self.assertEqual(stream.read(), expected)
                st = os.fstat(stream.fileno())

                def commit():
                    host_files.host_write(stream, raw)

                commit.inplace_identity = {"device": st.st_dev, "inode": st.st_ino}
                yield commit

        def tear(stream, raw):
            original = self.file.read_bytes()
            offset = (
                next(i for i, (a, b) in enumerate(zip(original, raw)) if a != b) + 17
            )
            stream.seek(0)
            stream.write(raw[:offset])
            stream.flush()
            os.fsync(stream.fileno())
            raise SystemExit("Synthetic in-place interruption")

        # Only settings host_apply uses this tear; native Hook installation has
        # completed before it, using the regular native writer.
        apply = host_files.host_apply

        def interrupt(rows, persist):
            if str(self.file) not in rows:
                return apply(rows, persist)
            with (
                patch.object(host_files, "host_stage", side_effect=inplace),
                patch.object(host_files, "host_write", side_effect=tear),
            ):
                return apply(rows, persist)

        with (
            patch.object(host_files, "host_apply", side_effect=interrupt),
            self.assertRaises(SystemExit),
        ):
            hooks.refresh(self.home)
        self.assertTrue(hooks.recover_update(self.home))
        self.assertEqual(metadata.private_json(self.file), self.value)
        self.assertEqual(hooks.refresh(self.home)["status"], "global-hook-updated")
        self.preserved()

    def test_frozen_code_tampering_is_rejected(self):
        hooks.refresh(self.home)
        root = next((self.file.parent / "code").iterdir())
        (root / "src/agent_brain_client/runtime/collection.py").write_bytes(b"Changed by outside actor")
        with self.assertRaises(ValueError):
            hooks.refresh(self.home)

    def test_arbitrary_events_same_event_groups_and_dotted_keys_are_preserved(self):
        import uuid
        event = 'Tool_' + uuid.uuid4().hex
        foreign = ('apps.' + event + '.enabled = false\n'
                   'apps.' + event + '.ratios = [nan, inf, -inf]\n'
                   'hooks.' + event + ' = [{hooks=[{type="command",command="synthetic-other"}]}]\n'
                   '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype="command"\ncommand="synthetic-stop"\n').encode()
        self.config.write_bytes(self.config.read_bytes().replace(hooks.BEGIN.encode(), hooks.BEGIN.encode() + b'\n' + foreign))
        expected = hooks.unowned_config(hooks.tomllib.loads(self.config.read_text()), hooks.block(self.value))
        self.assertTrue(hooks.installation_status(self.value))
        self.assertTrue(hooks.refresh(self.home, check_only=True)['update_available'])
        hooks.refresh(self.home)
        self.assertIn(foreign, self.config.read_bytes())
        self.assertTrue(hooks.installation_status(source_policy.load(self.home)))
        hooks.transact(self.native, self.file.parent, '', remove=True)
        self.assertTrue(hooks.typed_equal(hooks.tomllib.loads(self.config.read_text()), expected))
        self.preserved()

    def test_inline_hooks_and_marker_looking_string_data_are_semantic(self):
        config = hooks.tomllib.loads(self.config.read_text())
        config['hooks']['Stop'].insert(0, {'hooks': [{'type': 'command', 'command': 'synthetic-foreign'}]})
        raw = ('# synthetic comment\u2028' + hooks.BEGIN + '\n'
               + 'description = """\n' + hooks.BEGIN + '\n[hooks.Stop]\n' + hooks.END + '\n"""\n'
               + 'hooks = ' + host.toml_literal(config['hooks']) + '\n'
               + hooks.BEGIN + '\n' + hooks.END + '\n').encode()
        self.config.write_bytes(raw)
        expected = hooks.unowned_config(hooks.tomllib.loads(raw.decode()), hooks.block(self.value))
        hooks.refresh(self.home)
        self.assertTrue(hooks.installation_status(source_policy.load(self.home)))
        hooks.transact(self.native, self.file.parent, '', remove=True)
        actual = hooks.tomllib.loads(self.config.read_text())
        self.assertEqual(hooks.unowned_config(actual, None), hooks.unowned_config(expected, None))
        self.assertEqual(actual['description'], expected['description'])

    def inline_event_config(self, dotted):
        config = hooks.tomllib.loads(self.config.read_text())
        config['hooks']['OtherEvent'] = [{'hooks': [{'type': 'command', 'command': 'synthetic-foreign'}]}]
        config['hooks']['state'] = {'synthetic-stop': {'enabled': False, 'trusted_hash': 'synthetic-pin'}}
        prefix = self.config.read_text().split(hooks.BEGIN)[0]
        assignments = ''.join(('hooks.' if dotted else '') + event + ' = ' + host.toml_literal(groups) + '\n'
                              for event, groups in config['hooks'].items())
        return (prefix + hooks.BEGIN + '\n' + ('' if dotted else '[hooks]\n') + assignments + hooks.END + '\n').encode()

    def assert_inline_event_lifecycle(self, dotted):
        before = self.inline_event_config(dotted)
        self.config.write_bytes(before)
        expected = hooks.unowned_config(hooks.tomllib.loads(before.decode()), hooks.block(self.value))
        self.assertTrue(hooks.installation_status(self.value))
        self.assertTrue(hooks.refresh(self.home, check_only=True)['update_available'])
        self.assertEqual(self.config.read_bytes(), before)
        hooks.refresh(self.home)
        self.assertTrue(hooks.installation_status(source_policy.load(self.home)))
        hooks.transact(self.native, self.file.parent, '', remove=True)
        self.assertTrue(hooks.typed_equal(hooks.unowned_config(hooks.tomllib.loads(self.config.read_text()), None), expected))
        self.preserved()

    def test_inline_event_assignment_lifecycle(self):
        self.assert_inline_event_lifecycle(False)

    def test_dotted_inline_event_assignment_lifecycle(self):
        self.assert_inline_event_lifecycle(True)

    def test_inline_event_interruption_restores_exact_preimage(self):
        before = self.inline_event_config(False)
        self.config.write_bytes(before)
        target = hooks.block(dict(self.value, code_sha256={name: 'c' * 64 for name in hooks.FILES}))
        apply = host_files.host_apply

        def interrupt(rows, persist):
            apply(rows, persist)
            raise SystemExit('synthetic interrupted inline event write')

        with patch.object(host_files, 'host_apply', side_effect=interrupt), self.assertRaises(SystemExit):
            hooks.transact(self.native, self.file.parent, target)
        receipt = self.file.parent / 'hook-install.json'
        hooks.recover_record(self.config, receipt, hooks.read_receipt(receipt))
        self.assertEqual(self.config.read_bytes(), before)
        hooks.transact(self.native, self.file.parent, target)

    def test_foreign_command_inside_owned_group_survives_refresh_and_removal(self):
        foreign = (b'[[hooks.Stop.hooks]]\ntype="command"\n'
                   b'command="synthetic-shared-group-command"\n')
        before = self.config.read_bytes().replace(hooks.END.encode(), foreign + hooks.END.encode())
        self.config.write_bytes(before)
        self.assertTrue(hooks.installation_status(self.value))
        expected = hooks.unowned_config(hooks.tomllib.loads(before.decode()), hooks.block(self.value))
        hooks.refresh(self.home)
        self.assertIn(foreign, self.config.read_bytes())
        self.assertTrue(hooks.installation_status(source_policy.load(self.home)))
        hooks.transact(self.native, self.file.parent, '', remove=True)
        self.assertTrue(hooks.typed_equal(hooks.tomllib.loads(self.config.read_text()), expected))

    def test_concurrent_unrelated_edit_reloads_and_merges_without_losing_pause(self):
        stage = host_files.host_stage
        attempts = []
        off = self.file.parent / 'global.off'
        off.write_bytes(b'paused')

        @contextmanager
        def edit_once(path, *args):
            if path == self.config and not attempts:
                attempts.append(True)
                self.config.write_bytes(b'new_tool_setting = true\n' + self.config.read_bytes())
            with stage(path, *args) as commit: yield commit

        with patch.object(host_files, 'host_stage', side_effect=edit_once):
            hooks.refresh(self.home)
        self.assertIn(b'new_tool_setting = true\n', self.config.read_bytes())
        self.assertTrue(hooks.installation_status(source_policy.load(self.home)))
        self.assertEqual(off.read_bytes(), b'paused')
        self.preserved()

    def test_concurrent_managed_edit_is_preserved_and_names_exact_field(self):
        stage = host_files.host_stage
        edits = []

        @contextmanager
        def edit_once(path, *args):
            if path == self.config and not edits:
                self.config.write_bytes(self.config.read_bytes().replace(b'timeout = 120', b'timeout = 119'))
                edits.append(self.config.read_bytes())
            with stage(path, *args) as commit: yield commit

        with patch.object(host_files, 'host_stage', side_effect=edit_once):
            with self.assertRaisesRegex(ValueError, r'hooks.SessionStart\[0\].hooks\[0\].timeout'):
                hooks.refresh(self.home)
        self.assertEqual(self.config.read_bytes(), edits[0])
        self.assertEqual(metadata.private_json(self.file), self.value)

    @unittest.skipUnless(os.name == 'posix', 'Windows excludes competing writers while staged')
    def test_concurrent_edit_at_commit_discards_stale_journal_and_retries(self):
        stage = host_files.host_stage
        edits = []

        @contextmanager
        def edit_at_commit(path, *args):
            with stage(path, *args) as commit:
                def guarded():
                    if path == self.config and not edits:
                        edits.append(True)
                        self.config.write_bytes(b'commit_time_setting = true\n' + self.config.read_bytes())
                    return commit()
                yield guarded

        with patch.object(host_files, 'host_stage', side_effect=edit_at_commit):
            hooks.refresh(self.home)
        self.assertIn(b'commit_time_setting = true', self.config.read_bytes())
        self.assertTrue(hooks.installation_status(source_policy.load(self.home)))
        self.assertEqual(hooks.read_receipt(self.file.parent / 'hook-install.json')['phase'], 'complete')

    def test_linked_code_cache_is_rejected_without_writing_outside(self):
        outside = private_directory(self.home / "outside-cache")
        try:
            (self.file.parent / "code").symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Native token cannot create synthetic directory links")
        before = self.config.read_bytes()
        with self.assertRaises(ValueError):
            hooks.refresh(self.home)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(metadata.private_json(self.file), self.value)


class SemanticConfigMerge(unittest.TestCase):
    def test_del_values_and_keys_stay_valid_toml_when_reencoding_shared_assignments(self):
        import agent_brain_client.adapters.harnesses.codex.installation as ci
        from agent_brain_client.adapters.harnesses.codex.lifecycle import remove_table
        source = ('mcp_servers={agent_brain_server={command="project_sources"}, other={command="other", '
                  'env={TOKEN="\\u007f", "\\u007f"="\U0001f600", "\U0001f600"="value"}}}\n').encode()
        expected = hooks.tomllib.loads(source.decode())
        target = {'command': 'new', 'enabled': False}
        merged = ci.render(source, target, True)
        expected['mcp_servers']['agent_brain_server'] = target
        self.assertTrue(host.toml_equal(hooks.tomllib.loads(merged.decode()), expected))
        self.assertNotIn(b'\x7f', merged)
        removed = hooks.tomllib.loads(remove_table(merged).decode())
        self.assertEqual(removed['mcp_servers']['other'], expected['mcp_servers']['other'])
        for key in ('\x7f', '\U0001f600', 'ordinary'):
            self.assertEqual(hooks.tomllib.loads(host.toml_key(key) + '=true\n'), {key: True})

    def test_hash_only_migration_uses_authenticated_image_not_live_file_hash(self):
        import base64
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = private_directory(Path(temp.name).resolve() / 'state')
        config = state / 'config.toml'
        original = b'model="project_sources"\n[mcp_servers.agent_brain_server]\ncommand="fixed"\nenabled=true\n'
        current = original.replace(b'model="project_sources"', b'model="new"').replace(b'enabled=true', b'enabled=false')
        pin = host.sha(original)
        self.assertFalse(host.migration_config_matches(config, current, pin, state, {}))
        journal = {'before': {str(config): {'data': base64.b64encode(original).decode()}}}
        saved = state / 'native-install-receipt.json'
        metadata.write(saved, journal)
        self.assertTrue(host.migration_config_matches(config, current, pin, state, {}))
        self.assertFalse(host.migration_config_matches(config, current.replace(b'command="fixed"', b'command="changed"'), pin, state, {}))
        journal['before'][str(config)]['data'] = base64.b64encode(current).decode()
        metadata.write(saved, journal)
        self.assertFalse(host.migration_config_matches(config, current, pin, state, {}))

    def test_equivalent_mcp_syntax_and_shared_inline_tables(self):
        import agent_brain_client.adapters.harnesses.codex.installation as ci
        from agent_brain_client.adapters.harnesses.codex.lifecycle import remove_table
        import uuid
        name = 'tool_' + uuid.uuid4().hex + '\U0001f3a8'
        forms = [
            '["mcp_servers".\'agent_brain_server\']\ncommand="project_sources"\n',
            'mcp_servers.agent_brain_server.command="project_sources"\n',
            '[mcp_servers]\nagent_brain_server.command="project_sources"\n',
            'mcp_servers={agent_brain_server={command="project_sources"}, ' + json.dumps(name, ensure_ascii=False) + '={command="other"}}\n',
        ]
        target = {'command': 'new', 'enabled': False, 'env': {'z': 'keep', 'a': 'keep'}}
        for form in forms:
            with self.subTest(form=form):
                source = ('# Unrelated root data\nmodel="synthetic"\nforeign_ratio=nan\n' + form).encode()
                before = hooks.tomllib.loads(source.decode())
                expected = copy.deepcopy(before)
                expected['mcp_servers']['agent_brain_server'] = target
                merged = ci.render(source, target, True)
                self.assertTrue(hooks.typed_equal(hooks.tomllib.loads(merged.decode()), expected))
                # Serialization round-trips must produce identical plan images.
                self.assertEqual(merged, ci.render(source, json.loads(json.dumps(target, sort_keys=True)), True))
                removed = hooks.tomllib.loads(remove_table(merged).decode())
                self.assertNotIn('agent_brain_server', removed.get('mcp_servers', {}))
                self.assertEqual(removed['model'], before['model'])
                if name in before['mcp_servers']:
                    self.assertEqual(removed['mcp_servers'][name], before['mcp_servers'][name])

    def test_multiline_strings_and_array_table_paths_are_not_confused(self):
        source = ('text="""\n[hooks.Stop]\n# agent-brain-global-memory:begin\n"""\n'
                  '[[arbitrary]]\nname="one"\n[[arbitrary.child]]\nvalue=1\n'
                  '[[arbitrary]]\nname="two"\n[[arbitrary.child]]\nvalue=2\n')
        result = host.toml_remove(source, [('arbitrary', 0)])
        self.assertEqual(hooks.tomllib.loads(result), {'text': '[hooks.Stop]\n# agent-brain-global-memory:begin\n',
                                                      'arbitrary': [{'name': 'two', 'child': [{'value': 2}]}]})


if __name__ == "__main__":
    unittest.main()
