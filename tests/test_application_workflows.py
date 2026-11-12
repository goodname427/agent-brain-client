"""Application use cases against semantic ports, without host files or transports."""

from contextlib import nullcontext
import unittest
from agent_brain_client.application.installation.update import ClientUpdate
from agent_brain_client.application.installation.update_ports import (
    RegisteredInstallation,
    ManagedConfigurationConflict,
)
from agent_brain_client.application.synchronization.pull import ContentSynchronization


class RefreshPorts:
    def __init__(self):
        self.calls = []
        self.mapping = self
        self.sources = self
        self.pending = 0
        self.uncertain = False
        self.failure = None

    def ensure_refresh_available(self):
        pass

    def refresh_transaction(self):
        self.calls.append("lock")
        return nullcontext()

    def recover(self):
        self.calls.append("recover")
        if self.failure:
            raise self.failure

    def reconcile_pending(self):
        self.calls.append("imports")

    def replay_writes(self):
        self.calls.append("replay")

    def queue_status(self):
        return {"pending": self.pending, "conflict": 0, "blocked": 0}

    def import_status(self):
        return {"counts": {}}

    def owned_snapshot(self):
        self.calls.append("owned")
        return {"files": {}}

    def fetch_snapshot(self):
        self.calls.append("fetch")
        return {"records": []}

    def apply_snapshot(self, snapshot, owned, progress):
        self.calls.append("apply")
        return {"status": "content-current"}

    def recovery_uncertain(self):
        self.calls.append("recovery-outcome")
        return self.uncertain


class MaintenancePorts:
    mcp_config_field = "synthetic-mcp"
    check_command = "check"
    update_command = "update"

    def __init__(self):
        self.calls = []
        self.recovery = False
        self.pending_handover = False
        self.active = {"version": "1.0.0", "commit": "a" * 40}
        self.channel_result = "updated"
        self.hook_error = None

    def recover_hooks(self, *, check_only):
        self.calls.append(("recover-hooks", check_only))
        return self.recovery

    def handover_pending(self):
        return self.pending_handover

    def refresh_launcher(self, **kwargs):
        self.calls.append("launcher")
        return False

    def inspect(self):
        return {"registration_verified": True, "installation_state": "verified"}

    def load_registration(self):
        self.calls.append("registration")
        return RegisteredInstallation(
            "synthetic-platform", True, {"registered_tool_count": 1}, "codex"
        )

    def environment(self):
        return nullcontext()

    def open_channel(self):
        return self

    def verified_active(self):
        return self.active

    def discover(self, adapter, platform, installed):
        self.calls.append("discover")
        return {
            "status": "compatible-update",
            "releases": [{"manifest": {"version": "1.0.1"}, "commit": "b" * 40}],
        }

    def update(self):
        self.calls.append("update")
        if self.channel_result == "updated":
            self.active = {"version": "1.0.1", "commit": "b" * 40}
        return {"status": self.channel_result}

    def tool_discovery(self, active):
        return {
            "registration_update_required": False,
            "registered_enabled_tools": ["synthetic"],
        }

    def launcher_update_available(self, version, commit):
        return False

    def refresh_hooks(self, *, check_only=False):
        self.calls.append(("hooks", check_only))
        if self.hook_error:
            raise self.hook_error
        return {
            "status": "global-hook-current",
            "native_changed": False,
            "update_available": False,
        }

    def collection_installed(self):
        return True

    def install_collection(self):
        self.calls.append("install-hook")
        return {"native_changed": True}

    def setup_sources(self):
        self.calls.append("source-setup")
        return {"collection_enabled": True}


class ApplicationWorkflows(unittest.TestCase):
    def test_refresh_uses_owner_recovery_before_replay_and_mapping(self):
        ports = RefreshPorts()
        self.assertEqual(
            ContentSynchronization(ports).pull()["status"], "content-current"
        )
        self.assertEqual(
            ports.calls,
            [
                "lock",
                "recover",
                "imports",
                "replay",
                "imports",
                "owned",
                "fetch",
                "apply",
            ],
        )

    def test_pending_changes_prevent_snapshot_mapping_and_read_only_refresh_skips_replay(
        self,
    ):
        ports = RefreshPorts()
        ports.pending = 1
        value = ContentSynchronization(ports).pull(replay=False)
        self.assertEqual(value["status"], "content-local-changes-retained")
        self.assertEqual(ports.calls, ["lock", "recover", "imports", "imports"])

    def test_mapping_owner_determines_uncertain_native_outcome(self):
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain):
                ports = RefreshPorts()
                ports.failure = OSError("synthetic")
                ports.uncertain = uncertain
                value = ContentSynchronization(ports).pull()
                self.assertEqual(value["native_changed"], None if uncertain else False)
                self.assertEqual(value["reason"], "file-access-failed")
                self.assertEqual(ports.calls, ["lock", "recover", "recovery-outcome"])

    def test_check_update_reports_pending_recovery_without_mutating_or_discovering(
        self,
    ):
        for kind in ("hook", "launcher"):
            ports = MaintenancePorts()
            ports.recovery = kind == "hook"
            ports.pending_handover = kind == "launcher"
            value = ClientUpdate(ports).run("check-update")
            self.assertIsNone(value["native_changed"])
            self.assertEqual(ports.calls, [("recover-hooks", True)])

    def test_discovery_does_not_apply_candidate_or_setup_sources(self):
        ports = MaintenancePorts()
        value = ClientUpdate(ports).run("check-update")
        self.assertEqual(value["status"], "client-update-available")
        self.assertEqual(ports.active["version"], "1.0.0")
        self.assertIn(("hooks", True), ports.calls)
        self.assertNotIn("update", ports.calls)
        self.assertNotIn("source-setup", ports.calls)

    def test_update_recovers_before_channel_then_refreshes_hooks_and_sources(self):
        ports = MaintenancePorts()
        ports.pending_handover = True
        value = ClientUpdate(ports).run("update")
        self.assertEqual(value["status"], "client-update-updated")
        self.assertTrue(value["active_package_changed"])
        self.assertIsNone(value["reload_required"])
        self.assertEqual(
            ports.calls,
            [
                ("recover-hooks", False),
                "launcher",
                "registration",
                "launcher",
                "update",
                "registration",
                ("hooks", False),
                "source-setup",
            ],
        )

    def test_hook_conflict_preserves_updated_package_and_reports_exact_field(self):
        ports = MaintenancePorts()
        ports.hook_error = ManagedConfigurationConflict("synthetic-hook-field")
        value = ClientUpdate(ports).run("update")
        self.assertEqual(value["status"], "client-update-review-required")
        self.assertEqual(value["conflict_field"], "synthetic-hook-field")
        self.assertEqual(value["active_package_version"], "1.0.1")
        self.assertNotIn("source-setup", ports.calls)

    def test_unconfirmed_channel_keeps_original_package_and_skips_followup(self):
        ports = MaintenancePorts()
        ports.channel_result = "offline"
        value = ClientUpdate(ports).run("update")
        self.assertEqual(value["status"], "client-update-unavailable")
        self.assertEqual(value["active_package_version"], "1.0.0")
        self.assertNotIn(("hooks", False), ports.calls)
        self.assertNotIn("source-setup", ports.calls)


class EnrollmentPorts:
    def __init__(self):
        self.record = None
        self.code = "c" * 48
        self.credential = "s" * 48
        self.grant = {"actor": "synthetic", "scopes": ["read", "put"]}
        self.profile = {"grant": self.grant}
        self.calls = []
        self.lost_reply = None
        self.terminal = None
        self.nonce = None

    def lease(self):
        return nullcontext()

    def load(self):
        import copy

        return copy.deepcopy(self.record)

    def save(self, value, expected):
        import copy

        if self.record != expected:
            raise ValueError("Concurrent credential change")
        self.record = copy.deepcopy(value)

    def clear(self, expected):
        if self.record != expected:
            raise ValueError("Concurrent credential change")
        self.record = None
        self.calls.append("clear")

    def secure_input(self):
        self.calls.append("input")
        return self.code

    def prepare(self, code, nonce, profile):
        self.calls.append(("prepare", nonce))
        if self.nonce is not None and self.nonce != nonce:
            raise AssertionError("Retry changed enrollment nonce")
        self.nonce = nonce
        result = {
            "phase": "pending",
            "credential": self.credential,
            "nonce": nonce,
            "device_id": "d" * 32,
            "grant": self.grant,
        }
        if self.lost_reply == "prepare":
            self.lost_reply = None
            raise OSError("Lost prepare reply after durable Server acceptance")
        return result

    def confirm(self, credential, nonce):
        self.calls.append(("confirm", nonce))
        if self.terminal:
            raise self.terminal
        if self.lost_reply == "confirm":
            self.lost_reply = None
            raise OSError("Lost confirmation reply")
        return {"phase": "active", "device_id": "d" * 32, "grant": self.grant}

    def enrollment(self, profile_sha="a" * 64):
        from agent_brain_client.application.installation.enrollment import (
            DeviceEnrollment,
        )

        return DeviceEnrollment(
            self.profile, profile_sha, self, self, self.secure_input
        )


class EnrollmentWorkflow(unittest.TestCase):
    def test_restart_after_lost_reply_reuses_saved_nonce_and_never_prompts_twice(self):
        for phase in ("prepare", "confirm"):
            with self.subTest(phase=phase):
                ports = EnrollmentPorts()
                ports.lost_reply = phase
                self.assertEqual(
                    ports.enrollment().run()["status"], "pairing-resume-required"
                )
                before = ports.load()
                self.assertEqual(
                    before["phase"], "preparing" if phase == "prepare" else "pending"
                )
                self.assertEqual(ports.enrollment().run()["status"], "device-paired")
                self.assertEqual(ports.record["phase"], "active")
                self.assertEqual(ports.record["nonce"], before["nonce"])
                self.assertEqual(ports.calls.count("input"), 1)

    def test_wrong_profile_does_not_contact_server_or_change_existing_identity(self):
        ports = EnrollmentPorts()
        self.assertEqual(ports.enrollment().run()["status"], "device-paired")
        saved, calls = ports.load(), list(ports.calls)
        self.assertEqual(
            ports.enrollment("b" * 64).run()["status"], "pairing-resume-required"
        )
        self.assertEqual(ports.record, saved)
        self.assertEqual(ports.calls, calls)

    def test_unknown_confirmation_never_authorizes_terminal_reset(self):
        ports = EnrollmentPorts()
        ports.enrollment().run()
        saved = ports.load()
        ports.terminal = OSError("Uncertain transport outcome")
        value = ports.enrollment().reset_terminal(
            lambda *args: self.fail("Unknown outcome cannot ask for reset")
        )
        self.assertEqual(value["status"], "pairing-reset-blocked")
        self.assertEqual(ports.record, saved)

    def test_terminal_reset_cas_preserves_a_concurrently_changed_credential(self):
        from agent_brain_client.application.installation.pairing import (
            EnrollmentTerminal,
        )

        ports = EnrollmentPorts()
        ports.enrollment().run()
        ports.terminal = EnrollmentTerminal("device_revoked")

        def reviewed_reset(reason, device_id):
            ports.record = dict(ports.record, nonce="x" * 48)
            return True

        value = ports.enrollment().reset_terminal(reviewed_reset)
        self.assertEqual(value["status"], "pairing-reset-blocked")
        self.assertEqual(ports.record["nonce"], "x" * 48)
        self.assertNotIn("clear", ports.calls)
