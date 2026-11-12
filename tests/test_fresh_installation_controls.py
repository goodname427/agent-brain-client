"""Retain source pause/scope while discarding old device identity and approvals."""



import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_fresh_installation as fixture
from agent_brain_client.adapters.harnesses.codex.content_ports import CodexContentPorts
from agent_brain_client.adapters.packages.layout import PYTHON_FILES
from agent_brain_client.adapters.platforms.filesystem import private_directory
from agent_brain_client.adapters.storage import metadata
from agent_brain_client.adapters.storage.collection import settings_path
from agent_brain_client.adapters.storage.reinstallation import read_journal
from agent_brain_client.application.installation.fresh import finish_fresh_installation
from agent_brain_client.domain import contract
from agent_brain_client.runtime.fresh_installation import restore_source_routing
from agent_brain_client.runtime.installation_completion import source_setup


class FreshControls(unittest.TestCase):
    def setUp(self):
        self.f = fixture.FreshInstallation(
            "test_retains_old_files_and_queue_restores_only_memory_projection"
        )
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_original_disabled_mcp_is_recorded_before_native_detach(self):
        f = self.f
        f.config.write_bytes(
            f.config.read_bytes().replace(b"enabled = true", b"enabled = false")
        )
        f.prepare()
        self.assertFalse(read_journal(f.home)["service_enabled"])
        self.assertNotIn("agent_brain_server", f.config.read_text())

    def test_orphan_global_stop_fence_refuses_retention_without_losing_state(self):
        f = self.f
        private_directory(settings_path(f.home).parent)
        (settings_path(f.home).parent / "global.off").touch(mode=0o600)
        before = f.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "stop fence"):
            f.prepare()
        self.assertEqual(f.config.read_bytes(), before)
        self.assertTrue(f.profile.exists())

    def test_shared_gate_blocks_session_and_global_capture_before_credentials(self):
        from agent_brain_client.adapters.storage.content_session import (
            ContentSession,
        )
        from agent_brain_client.adapters.server.collection import CollectionClient
        from agent_brain_client.application.installation.fresh import (
            FreshMemoryVerificationRequired,
        )
        from test_content_outbox import Cloud

        f = self.f
        record = f.port.plan()
        cloud = Cloud()
        cloud.grant = copy.deepcopy(f.grant)
        with f.port.lease():
            f.port.save(record)
        before = f.memory_path.read_bytes()
        for phase in ("prepared", "detaching", "archiving", "ready", "complete"):
            with self.subTest(phase=phase):
                record["phase"] = phase
                f.port.save(record)
                with self.assertRaises(FreshMemoryVerificationRequired):
                    ContentSession(
                        f.home, f.cache, f.content, cloud, host=CodexContentPorts, native_home=f.native
                    )
                with self.assertRaises(FreshMemoryVerificationRequired):
                    CollectionClient({"device_home": str(f.home)})
                with self.assertRaises(FreshMemoryVerificationRequired):
                    source_setup(f.home)
                self.assertEqual(f.memory_path.read_bytes(), before)

    def reconnect(self, *, enabled=True, stopped=False):
        f = self.f
        scope = {
            "schema": 1,
            "profile_id": "1" * 32,
            "all_projects": False,
            "include_roots": [
                {"root": str(f.home / "only-this-project"), "sharing": "project"}
            ],
            "exclude_roots": [str(f.home / "private")],
            "non_project_roots": [],
            "recall_personal": False,
        }
        settings = {
            "schema": 1,
            "enabled": enabled,
            "device_home": str(f.home),
            "scope": scope,
            "approved_scope_sha256": "a" * 64,
            "server_scope_sha256": "a" * 64,
            "legacy_projects": [],
            "code_sha256": {name: "a" * 64 for name in PYTHON_FILES},
            "operator": None,
            "operator_sha256": None,
        }
        private_directory(settings_path(f.home).parent)
        metadata.write(settings_path(f.home), settings)
        if stopped:
            (settings_path(f.home).parent / "global.off").touch(mode=0o600)
        f.prepare()
        record = read_journal(f.home)
        private_directory(f.profile.parent)
        profile = dict(f.old_profile, instance_id=record["id"])
        metadata.write(f.profile, profile)
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
        settings.update(
            enabled=False, approved_scope_sha256=None, server_scope_sha256=None
        )
        settings["scope"] = dict(
            scope,
            profile_id="2" * 32,
            all_projects=True,
            include_roots=[],
            exclude_roots=[],
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
        restore_source_routing(f.home)
        result = metadata.private_json(settings_path(f.home))
        self.assertEqual(result["scope"], dict(scope, profile_id="2" * 32))
        self.assertEqual(result["enabled"], enabled)
        self.assertIsNone(result["approved_scope_sha256"])
        self.assertIsNone(result["server_scope_sha256"])
        self.assertEqual(
            (settings_path(f.home).parent / "global.off").exists(), stopped
        )
        archived = Path(record["archive"]) / "global-memory"
        self.assertEqual((archived / "global.off").exists(), stopped)
        return result

    def test_disabled_source_stays_paused_without_register_or_resume(self):
        self.reconnect(enabled=False)
        with patch(
            "agent_brain_client.runtime.source_enrollment.configure",
            side_effect=AssertionError("Paused source must not auto-register/resume"),
        ):
            result = source_setup(self.f.home)
        self.assertEqual(result["status"], "retained-source-paused")
        self.assertFalse(result["collection_enabled"])

    def test_global_stop_fence_stays_stopped_without_register_or_resume(self):
        self.reconnect(stopped=True)
        with patch(
            "agent_brain_client.runtime.source_enrollment.configure",
            side_effect=AssertionError("Stopped source must not auto-register/resume"),
        ):
            result = source_setup(self.f.home)
        self.assertEqual(result["status"], "retained-source-paused")
        self.assertTrue((settings_path(self.f.home).parent / "global.off").exists())

    def test_narrow_scope_is_used_by_real_configuration_without_all_projects_expansion(
        self,
    ):
        value = self.reconnect()
        from agent_brain_client.runtime import source_enrollment

        state = {"global_profile_id": "2" * 32, "source_registered": False}
        packets = []
        client = SimpleNamespace(device_id="synthetic-new-source")

        def register(value, packet):
            packets.append(packet)
            return {"activated": False, "status": "synthetic-registration-pending"}

        with (
            patch.object(
                source_enrollment, "preflight", return_value=(client, state, None)
            ),
            patch.object(source_enrollment, "register", side_effect=register),
        ):
            source_setup(self.f.home)
        self.assertEqual(packets[0]["scope"], value["scope"])
        self.assertFalse(packets[0]["scope"]["all_projects"])

    def test_pause_created_after_confirmation_refuses_detach(self):
        f = self.f
        # A stop marker appearing after the retention plan must not be lost.
        private_directory(settings_path(f.home).parent)
        metadata.write(
            settings_path(f.home),
            {
                "schema": 1,
                "enabled": True,
                "device_home": str(f.home),
                "scope": {
                    "schema": 1,
                    "profile_id": "1" * 32,
                    "all_projects": False,
                    "include_roots": [],
                    "exclude_roots": [],
                    "non_project_roots": [],
                    "recall_personal": False,
                },
            },
        )

        def stop(_):
            (settings_path(f.home).parent / "global.off").touch(mode=0o600)
            return "FRESH"

        before = f.config.read_bytes()
        with self.assertRaisesRegex(ValueError, "pause or scope changed"):
            f.prepare(stop)
        self.assertEqual(f.config.read_bytes(), before)
        self.assertTrue(f.profile.exists())


if __name__ == "__main__":
    unittest.main()
