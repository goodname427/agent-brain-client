"""Both native host writers, fictional metadata, no server or credential IO."""

import copy
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from brain_lock import private_directory
import codex_connect as host
import global_memory as gm
import global_memory_install as hooks
import memory_automation as old


class GlobalMemoryUpdate(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="synthetic-hook-update-")
        self.addCleanup(temp.cleanup)
        self.home = private_directory(Path(temp.name).resolve() / "fixture")
        self.native = private_directory(self.home / "codex")
        self.file = gm.settings_path(self.home)
        private_directory(self.file.parent)
        state = self.file.parent.parent
        for name in ("profile.json", "content.json"):
            old.write(state / name, {"schema": 1})
        policy = {
            "capabilities": [
                "codex-stdio-mcp",
                "personal-content-explicit",
                "https-system-credential",
            ],
            "runtime": {
                "codex_home": str(self.native),
                "https_profile": str(state / "profile.json"),
                "https_profile_sha256": old.digest(
                    (state / "profile.json").read_bytes()
                ),
                "content_policy": str(state / "content.json"),
                "content_policy_sha256": old.digest(
                    (state / "content.json").read_bytes()
                ),
            },
        }
        old.write(state / "policy.json", policy)
        old.write(
            state / "codex-mcp.json",
            {
                "release_policy": str(state / "policy.json"),
                "release_policy_sha256": old.digest(
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
            "code_sha256": {name: "b" * 64 for name in gm.FILES},
        }
        old.write(self.file, self.value)
        self.config = self.native / "config.toml"
        old.write(self.config, {})
        self.config.write_bytes(b'# User setting\nmodel = "synthetic"\n')
        hooks.transact(self.native, self.file.parent, hooks.block(self.value))
        self.no_client = patch.object(
            gm, "GlobalClient", side_effect=AssertionError("No credential activation")
        )
        self.no_client.start()
        self.addCleanup(self.no_client.stop)

    def preserved(self):
        actual = old.private_json(self.file)
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
        self.assertTrue(hooks.installation_status(gm.load(self.home)))
        root = next((self.file.parent / "code").iterdir())
        code = 'import sys;sys.path.insert(0,sys.argv[1]);import global_memory as g;g.old.imports();import content_mcp,global_memory_install;g.load(sys.argv[2]);print("frozen-runtime-loaded")'
        environment = dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.native))
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                code,
                str(root / "tools"),
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
        old.write(self.file, self.value)
        off = self.file.parent / "global.off"
        off.write_bytes(b"Paused by user")
        hooks.refresh(self.home)
        self.preserved()
        self.assertEqual(off.read_bytes(), b"Paused by user")
        self.assertFalse(gm.approved(gm.load(self.home)))

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

    def test_foreign_hook_edit_is_preserved(self):
        self.config.write_bytes(
            self.config.read_bytes().replace(b"timeout = 120", b"timeout = 119")
        )
        before = self.config.read_bytes()
        with self.assertRaises(ValueError):
            hooks.refresh(self.home)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(old.private_json(self.file), self.value)

    def test_retained_removal_requires_authenticated_journal_and_exact_block(self):
        import client_lifecycle as lifecycle
        outside, previous = hooks.split_owned(self.config.read_bytes())
        self.config.write_bytes(outside.encode())
        with self.assertRaises((ValueError, OSError)):
            hooks.refresh(self.home)
        for phase, block, accepted in (('applying', previous, False),
                                        ('complete', 'Changed block', False),
                                        ('complete', previous, True),
                                        ('reinstalled', previous, True)):
            with (patch.object(lifecycle, 'read_removal', return_value={'phase': phase, 'hook_block': block}),
                  patch.object(lifecycle, 'validate_journal') as authenticate):
                if accepted:
                    self.assertEqual(hooks.refresh(self.home)['status'], 'global-hook-not-installed')
                else:
                    with self.assertRaises(ValueError):
                        hooks.refresh(self.home)
                authenticate.assert_called_once()
        self.assertEqual(self.config.read_bytes(), outside.encode())
        self.assertEqual(old.private_json(self.file), self.value)

    def test_interrupted_settings_write_is_recovered_before_retry(self):
        apply = host.host_apply

        def interrupt(rows, persist):
            apply(rows, persist)
            if str(self.file) in rows:
                raise SystemExit("Synthetic interruption after settings write")

        with (
            patch.object(host, "host_apply", side_effect=interrupt),
            self.assertRaises(SystemExit),
        ):
            hooks.refresh(self.home)
        self.assertTrue(hooks.recover_update(self.home, check_only=True))
        self.assertEqual(
            hooks.read_receipt(self.file.parent / "code-refresh.json")["phase"],
            "applying",
        )
        self.assertTrue(hooks.recover_update(self.home))
        self.assertEqual(old.private_json(self.file), self.value)
        self.assertEqual(hooks.refresh(self.home)["status"], "global-hook-updated")
        self.preserved()

    def test_outside_settings_edit_after_interruption_is_preserved(self):
        apply = host.host_apply

        def interrupt(rows, persist):
            apply(rows, persist)
            if str(self.file) in rows:
                raise SystemExit("Synthetic interruption")

        with (
            patch.object(host, "host_apply", side_effect=interrupt),
            self.assertRaises(SystemExit),
        ):
            hooks.refresh(self.home)
        value = copy.deepcopy(old.private_json(self.file))
        value["enabled"] = True
        old.write(self.file, value)
        with self.assertRaises(ValueError):
            hooks.recover_update(self.home)
        self.assertEqual(old.private_json(self.file), value)

    def test_recognized_torn_settings_write_recovers_original_then_retries(self):
        stage = host.host_stage

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
                    host.host_write(stream, raw)

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
        apply = host.host_apply

        def interrupt(rows, persist):
            if str(self.file) not in rows:
                return apply(rows, persist)
            with (
                patch.object(host, "host_stage", side_effect=inplace),
                patch.object(host, "host_write", side_effect=tear),
            ):
                return apply(rows, persist)

        with (
            patch.object(host, "host_apply", side_effect=interrupt),
            self.assertRaises(SystemExit),
        ):
            hooks.refresh(self.home)
        self.assertTrue(hooks.recover_update(self.home))
        self.assertEqual(old.private_json(self.file), self.value)
        self.assertEqual(hooks.refresh(self.home)["status"], "global-hook-updated")
        self.preserved()

    def test_frozen_code_tampering_is_rejected(self):
        hooks.refresh(self.home)
        root = next((self.file.parent / "code").iterdir())
        (root / "tools/global_memory.py").write_bytes(b"Changed by outside actor")
        with self.assertRaises(ValueError):
            hooks.refresh(self.home)

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
        self.assertEqual(old.private_json(self.file), self.value)


if __name__ == "__main__":
    unittest.main()
