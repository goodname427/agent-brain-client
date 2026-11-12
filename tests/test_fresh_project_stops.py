"""Reinstallation preserves project stops through the real native Hook CLI."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_fresh_installation as fixture
from agent_brain_client.adapters.packages.layout import PYTHON_FILES
from agent_brain_client.adapters.platforms.filesystem import private_directory
from agent_brain_client.adapters.storage import metadata
from agent_brain_client.adapters.storage.collection import settings_path, approved
from agent_brain_client.adapters.storage.reinstallation import (
    read_journal,
    RetainedInstallation,
)
from agent_brain_client.application.installation.fresh import (
    finish_fresh_installation,
    FreshSourceRoutingRequired,
)
from agent_brain_client.domain import contract
from agent_brain_client.domain.scopes import digest
from agent_brain_client.runtime.fresh_installation import (
    retained_installation,
    restore_source_routing,
)

ROOT = Path(__file__).resolve().parents[1]


class ProjectStops(unittest.TestCase):
    def setUp(self):
        self.f = fixture.FreshInstallation(
            "test_retains_old_files_and_queue_restores_only_memory_projection"
        )
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.project = self.f.home / "paused-project"
        self.allowed = self.f.home / "allowed-project"
        self.outside = self.f.home / "outside-project"
        for root in (self.project, self.allowed, self.outside):
            private_directory(root / ".git")
        private_directory(self.project / "subdirectory")
        self.controls = self.f.root / "project-memory/paused"
        self.off = self.controls / "memory.off"
        self.settings = self.controls / "settings.json"
        self.pins = {
            name: metadata.digest((ROOT / name).read_bytes()) for name in PYTHON_FILES
        }

    def original(
        self,
        *,
        marker=True,
        enabled=True,
        narrow=False,
        global_settings=True,
        delegated=True,
        external=False,
    ):
        f = self.f
        if external:
            self.controls = f.home / "external-project-controls"
            self.off, self.settings = (
                self.controls / "memory.off",
                self.controls / "settings.json",
            )
        private_directory(self.controls)
        metadata.write(
            self.settings,
            {
                "schema": 1,
                "enabled": enabled,
                "device_home": str(f.home),
                "project_root": str(self.project),
                "off_file": str(self.off),
                "source_mode": "codex-project",
                "profile": "SYNTHETIC_OLD_DEVICE_NOT_REUSED",
                "code_sha256": {"old-layout.py": "a" * 64},
            },
        )
        if marker:
            self.off.touch(mode=0o600)
        self.scope = {
            "schema": 1,
            "profile_id": "1" * 32,
            "all_projects": not narrow,
            "include_roots": [
                {"root": str(self.allowed), "sharing": "project"},
                {"root": str(self.project), "sharing": "project"},
            ],
            "exclude_roots": [str(f.home / "private")],
            "non_project_roots": [],
            "recall_personal": False,
        }
        self.old_settings = {
            "schema": 1,
            "enabled": True,
            "device_home": str(f.home),
            "scope": self.scope,
            "approved_scope_sha256": digest(self.scope),
            "server_scope_sha256": digest(self.scope),
            "legacy_projects": [
                {
                    "root": str(self.project),
                    "settings": str(self.settings),
                    "settings_sha256": metadata.digest(self.settings.read_bytes()),
                    "capture_delegated": delegated,
                }
            ],
            "code_sha256": self.pins,
            "operator": None,
            "operator_sha256": None,
        }
        if global_settings:
            private_directory(settings_path(f.home).parent)
            metadata.write(settings_path(f.home), self.old_settings)

    def connect(self):
        f = self.f
        record = read_journal(f.home)
        private_directory(f.profile.parent)
        metadata.write(f.profile, dict(f.old_profile, instance_id=record["id"]))
        metadata.write(f.root / "first-install/content-policy.json", f.content)
        policy = copy.deepcopy(f.policy)
        policy["runtime"]["https_profile_sha256"] = metadata.digest(
            f.profile.read_bytes()
        )
        metadata.write(f.root / "first-install/release-policy.json", policy)
        metadata.write(
            f.root / "codex-mcp.json",
            dict(
                f.metadata,
                release_policy_sha256=metadata.digest(
                    (f.root / "first-install/release-policy.json").read_bytes()
                ),
            ),
        )
        # Same initial state as the new Hook installer: no old IDs/approvals or
        # executable project configuration is copied into the new installation.
        settings = copy.deepcopy(self.old_settings)
        settings.update(
            enabled=False,
            legacy_projects=[],
            approved_scope_sha256=None,
            server_scope_sha256=None,
        )
        settings["scope"] = dict(
            self.scope,
            profile_id="2" * 32,
            all_projects=True,
            include_roots=[],
            exclude_roots=[],
            recall_personal=True,
        )
        private_directory(settings_path(f.home).parent)
        metadata.write(settings_path(f.home), settings)

        class Reader:
            def request(self, request):
                return {
                    "request_id": request["request_id"],
                    "result": contract.envelope(
                        "memory", "personal-owner", fixture.MEMORY
                    ),
                }

        finish_fresh_installation(
            f.port,
            Reader(),
            policy["runtime"]["https_profile_sha256"],
            ["personal-owner"],
        )

    def admit(self):
        value = metadata.private_json(settings_path(self.f.home))
        value.update(
            enabled=True,
            approved_scope_sha256=digest(value["scope"]),
            server_scope_sha256=digest(value["scope"]),
        )
        metadata.write(settings_path(self.f.home), value)
        return value

    def hook(self, root, event="UserPromptSubmit"):
        # This is the real public CLI, parsing, code/registration validation,
        # origin discovery and Hook routing in a NEW process. No module or
        # transport is patched in the child. The synthetic profile deliberately
        # cannot activate credentials if routing incorrectly reaches capture.
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(ROOT / "src/agent_brain_client/interfaces/cli/collection.py"),
                "global-hook",
                "--device-home",
                str(self.f.home),
            ],
            cwd=root,
            input=json.dumps(
                {
                    "cwd": str(root),
                    "hook_event_name": event,
                    "session_id": "synthetic-project-session",
                    "turn_id": "synthetic-project-turn",
                    "prompt": "SYNTHETIC_PROJECT_STOP_CHECK",
                }
            ),
            text=True,
            capture_output=True,
            timeout=20,
            env=dict(
                os.environ,
                HOME=str(self.f.home),
                USERPROFILE=str(self.f.home),
                CODEX_HOME=str(self.f.native),
            ),
        )

    def assert_quiet_hook(self, root):
        for event in ("UserPromptSubmit", "Stop"):
            result = self.hook(root, event)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {})
            for line in result.stderr.splitlines():
                diagnostic = json.loads(line)
                self.assertEqual(diagnostic["operation"], "global-hook-timing")
                self.assertEqual(set(diagnostic), {"operation", "timing_ms"})

    def assert_preserved(self):
        f = self.f
        record = read_journal(f.home)
        value = metadata.private_json(settings_path(f.home))
        self.assertIn(str(self.project), value["scope"]["exclude_roots"])
        self.assertEqual(value["scope"]["include_roots"], self.scope["include_roots"])
        self.assertEqual(value["scope"]["all_projects"], self.scope["all_projects"])
        self.assertEqual(value["scope"]["profile_id"], "2" * 32)
        self.assertEqual(value["legacy_projects"], [])
        self.assertEqual(
            record["project_controls"]["blocked_roots"], [str(self.project)]
        )
        archive = Path(record["archive"])
        self.assertEqual(
            (
                archive
                / "first-install/content-cache/outbox/retained_original_request.json"
            ).read_bytes(),
            b"SYNTHETIC_UNSENT_OLD_QUEUE_DO_NOT_REPLAY",
        )
        self.assertFalse((f.cache / "outbox").exists())

    def test_real_hook_restarts_keep_stopped_project_and_subdirectories_excluded(self):
        self.original()
        self.f.prepare()
        self.connect()
        restore_source_routing(self.f.home)
        self.assert_preserved()
        self.assertTrue(
            (
                Path(read_journal(self.f.home)["archive"])
                / "project-memory/paused/memory.off"
            ).exists()
        )
        self.assertTrue(approved(self.admit()))
        self.assert_quiet_hook(self.project)
        self.assert_quiet_hook(self.project / "subdirectory")
        self.assert_quiet_hook(self.project)  # another process restart
        # Positive control: an allowed project reaches the credential boundary,
        # so stopped hooks were excluded, not globally disabled or stubbed out.
        result = self.hook(self.allowed)
        self.assertIn('"status": "capture-unconfirmed"', result.stderr)
        self.assertIn('"error_type": "ValueError"', result.stderr)

    def test_narrow_scope_does_not_expand_while_project_stop_is_added(self):
        self.original(narrow=True)
        self.f.prepare()
        self.connect()
        restore_source_routing(self.f.home)
        self.assert_preserved()
        self.admit()
        self.assert_quiet_hook(self.project)
        self.assert_quiet_hook(self.outside)
        self.assertIn(
            '"status": "capture-unconfirmed"', self.hook(self.allowed).stderr
        )

    def test_disabled_project_and_external_reference_keep_stop_without_old_identity(
        self,
    ):
        self.original(marker=False, enabled=False, external=True)
        before = self.settings.read_bytes()
        self.f.prepare()
        self.connect()
        restore_source_routing(self.f.home)
        self.assert_preserved()
        self.admit()
        self.assert_quiet_hook(self.project)
        self.assertEqual(self.settings.read_bytes(), before)

    def test_interrupted_archive_restores_project_controls_from_journal_after_restart(
        self,
    ):
        from agent_brain_client.adapters.storage import reinstallation

        self.original()
        move = reinstallation.retain_item

        def interrupted(source, target, identity):
            move(source, target, identity)
            if source.name == "project-memory":
                raise SystemExit("synthetic crash after project controls moved")

        with (
            patch.object(reinstallation, "retain_item", side_effect=interrupted),
            self.assertRaises(SystemExit),
        ):
            self.f.prepare()
        self.assertEqual(read_journal(self.f.home)["phase"], "archiving")
        self.f.port = retained_installation(self.f.home, self.f.native)
        self.f.prepare()
        self.connect()
        restore_source_routing(self.f.home)
        self.assert_preserved()
        self.admit()
        self.assert_quiet_hook(self.project)

    def test_failed_routing_restore_blocks_capture_then_resumes_same_state(self):
        from agent_brain_client.adapters.storage import collection

        self.original()
        self.f.prepare()
        self.connect()
        with (
            patch.object(
                collection,
                "seed_project_stops",
                side_effect=OSError("synthetic write failure"),
            ),
            self.assertRaises(OSError),
        ):
            restore_source_routing(self.f.home)
        self.assertFalse(read_journal(self.f.home).get("routing_restored", False))
        value = (
            self.admit()
        )  # even a prematurely admitted settings image cannot bypass the journal
        self.assertFalse(approved(value))
        self.assert_quiet_hook(self.project)
        from agent_brain_client.adapters.server.collection import CollectionClient

        with self.assertRaises(FreshSourceRoutingRequired):
            CollectionClient(value)
        value.update(approved_scope_sha256=None, server_scope_sha256=None)
        metadata.write(settings_path(self.f.home), value)
        restore_source_routing(self.f.home)
        self.assert_preserved()
        self.admit()
        self.assert_quiet_hook(self.project)

    def test_crash_after_exclusion_write_before_completion_flag_is_idempotent(self):
        self.original()
        self.f.prepare()
        self.connect()
        save = RetainedInstallation.save

        def interrupted(port, record):
            if record.get("routing_restored"):
                raise SystemExit("synthetic crash before completion proof")
            return save(port, record)

        with (
            patch.object(RetainedInstallation, "save", interrupted),
            self.assertRaises(SystemExit),
        ):
            restore_source_routing(self.f.home)
        self.assertFalse(read_journal(self.f.home).get("routing_restored", False))
        restore_source_routing(self.f.home)
        first = metadata.private_json(settings_path(self.f.home))
        restore_source_routing(self.f.home)
        self.assertEqual(metadata.private_json(settings_path(self.f.home)), first)
        self.assert_preserved()
        self.admit()
        self.assert_quiet_hook(self.project)

    def test_marker_created_during_confirmation_refuses_detach(self):
        self.original(marker=False)
        before = self.f.config.read_bytes()

        def stop(_):
            self.off.touch(mode=0o600)
            return "FRESH"

        with self.assertRaisesRegex(ValueError, "project stop state changed"):
            self.f.prepare(stop)
        self.assertEqual(self.f.config.read_bytes(), before)
        self.assertTrue(self.f.profile.exists())

    def test_explicit_global_handover_does_not_misread_retired_marker_as_user_stop(
        self,
    ):
        self.original(delegated=False)
        self.f.prepare()
        self.assertEqual(
            read_journal(self.f.home)["project_controls"]["blocked_roots"], []
        )

    def test_project_stop_without_previous_global_settings_is_preserved(self):
        self.original(global_settings=False)
        self.f.prepare()
        self.connect()
        restore_source_routing(self.f.home)
        self.assertIsNone(read_journal(self.f.home)["routing"])
        self.admit()
        self.assert_quiet_hook(self.project)
        self.assertIn(
            '"status": "capture-unconfirmed"', self.hook(self.allowed).stderr
        )

    def test_unrepresentable_exclusion_scope_refuses_before_archiving(self):
        self.original()
        value = metadata.private_json(settings_path(self.f.home))
        value["scope"]["exclude_roots"] = [
            str(self.f.home / ("excluded-" + str(n))) for n in range(128)
        ]
        metadata.write(settings_path(self.f.home), value)
        before = self.f.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "scope capacity"):
            self.f.prepare()
        self.assertIsNone(read_journal(self.f.home))
        self.assertEqual(self.f.config.read_bytes(), before)
        self.assertTrue(self.f.profile.exists())
        self.assertTrue(self.off.exists())


if __name__ == "__main__":
    unittest.main()
