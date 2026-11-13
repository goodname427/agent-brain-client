"""Collection stop/resume decisions without native files or a credential store."""

import unittest
from agent_brain_client.application.collection.control import CollectionControl


class ControlPorts:
    def __init__(self):
        self.calls = []
        self.fenced = False
        self.reply = {"result": {"schema": 1, "stopped": False}}
        self.reasons = ["local-stop-fence"]
        self.project_failure = False

    def set_fence(self, value):
        self.fenced = True
        self.calls.append("fence")

    def clear_fence(self, value):
        self.calls.append("clear")
        self.fenced = False

    def admission(self, value):
        return {"reasons": self.reasons, "next_action": "synthetic-review"}

    def remote(self, value, method):
        if not self.fenced:
            raise AssertionError("Remote control before local fence")
        self.calls.append(method)
        return self.reply

    def fence_project(self, row):
        self.calls.append("fence-" + row["root"])

    def stop_project(self, row):
        self.calls.append("stop-" + row["root"])
        if self.project_failure:
            raise OSError("Unconfirmed project control")

    def resume_project(self, row):
        self.calls.append("resume-" + row["root"])
        if self.project_failure:
            raise OSError("Unconfirmed project control")

    def save_settings(self, value):
        self.calls.append("settings")


class CollectionControlContract(unittest.TestCase):
    def value(self, admitted=True):
        return {
            "server_scope_sha256": "a" * 64 if admitted else None,
            "legacy_projects": [
                {"root": "delegated"},
                {"root": "migrated", "capture_delegated": False},
            ],
        }

    def test_stop_fences_every_project_before_global_remote(self):
        for reply in ({"result": {}}, {"error": {"code": "unconfirmed"}}):
            ports = ControlPorts()
            ports.reply = reply
            result = CollectionControl(ports).stop(self.value())
            self.assertEqual(result["remote_confirmed"], "error" not in reply)
            self.assertEqual(result["status"], "global-memory-stop-unconfirmed" if "error" in reply else "global-memory-stopped")
            self.assertEqual(ports.calls, ["fence", "fence-delegated", "fence-migrated", "memory_global_stop"])
            self.assertTrue(ports.fenced)

    def test_project_stop_attempts_every_local_fence_then_every_remote(self):
        for failed in (False, True):
            ports = ControlPorts()
            ports.project_failure = failed
            result = CollectionControl(ports).stop(self.value(False))
            self.assertEqual(ports.calls, ["fence", "fence-delegated", "fence-migrated", "stop-delegated", "stop-migrated"])
            self.assertEqual(result["remote_confirmed"], not failed)
            self.assertEqual(result["status"], "global-memory-stop-unconfirmed" if failed else "global-memory-stopped")
            self.assertTrue(ports.fenced)
            self.assertTrue(all(p["local_stop_fence"] for p in result["projects"]))

    def test_local_failure_does_not_skip_other_local_or_remote_stops(self):
        ports = ControlPorts()
        def fence(row):
            ports.calls.append("fence-" + row["root"])
            if row["root"] == "delegated":
                raise OSError("Synthetic write denied")
        ports.fence_project = fence
        result = CollectionControl(ports).stop(self.value(False))
        self.assertEqual(ports.calls, ["fence", "fence-delegated", "fence-migrated", "stop-delegated", "stop-migrated"])
        self.assertEqual(result["status"], "global-memory-stop-unconfirmed")
        self.assertEqual(result["projects"][0]["error_stage"], "local-stop")
        self.assertFalse(result["projects"][0]["local_stop_fence"])
        self.assertTrue(result["projects"][1]["local_stop_fence"])

    def test_unexpected_remote_failure_is_reported_and_remaining_projects_attempted(self):
        ports = ControlPorts()
        def stop(row):
            ports.calls.append("stop-" + row["root"])
            if row["root"] == "delegated":
                raise RuntimeError("Synthetic unexpected remote failure")
        ports.stop_project = stop
        result = CollectionControl(ports).stop(self.value(False))
        self.assertEqual(ports.calls[-2:], ["stop-delegated", "stop-migrated"])
        self.assertEqual(result["status"], "global-memory-stop-unconfirmed")
        self.assertEqual(result["projects"][0]["remote_error_type"], "RuntimeError")
        self.assertTrue(result["projects"][1]["remote_confirmed"])

    def test_resume_clears_fence_only_after_confirmed_server_response(self):
        for reply in (
            {"result": {"schema": 1, "stopped": False}},
            {"result": {"schema": 1, "stopped": True}},
            {"error": {"code": "unconfirmed"}},
        ):
            ports = ControlPorts()
            ports.reply = reply
            result = CollectionControl(ports).resume(self.value())
            confirmed = "error" not in reply and reply["result"]["stopped"] is False
            self.assertEqual(result["remote_confirmed"], confirmed)
            self.assertEqual(ports.fenced, not confirmed)
            self.assertEqual(
                ports.calls,
                ["fence", "memory_global_resume"] + (["clear"] if confirmed else []),
            )

    def test_resume_does_not_expand_unapproved_scope_and_skips_migrated_projects(self):
        ports = ControlPorts()
        value = CollectionControl(ports).resume(self.value(False))
        self.assertEqual(ports.calls, ["fence", "resume-delegated", "clear"])
        self.assertFalse(value["collection_enabled"])
        ports = ControlPorts()
        ports.reasons = ["scope-not-approved"]
        value = CollectionControl(ports).resume(self.value())
        self.assertEqual(ports.calls, [])
        self.assertEqual(value["status"], "global-memory-activation-required")

    def test_invalid_recall_options_never_write_native_settings(self):
        ports = ControlPorts()
        with self.assertRaises(ValueError):
            CollectionControl(ports).configure_recall({}, {"maxResults": 0})
        self.assertEqual(ports.calls, [])
