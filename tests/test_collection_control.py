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

    def test_stop_sets_local_fence_before_remote_and_keeps_it_on_uncertainty(self):
        for reply in ({"result": {}}, {"error": {"code": "unconfirmed"}}):
            ports = ControlPorts()
            ports.reply = reply
            if "error" in reply:
                with self.assertRaises(ValueError):
                    CollectionControl(ports).stop(self.value())
            else:
                self.assertTrue(
                    CollectionControl(ports).stop(self.value())["remote_confirmed"]
                )
            self.assertEqual(ports.calls, ["fence", "memory_global_stop"])
            self.assertTrue(ports.fenced)

    def test_project_stop_preserves_existing_all_rows_behavior_and_fence_on_error(self):
        ports = ControlPorts()
        result = CollectionControl(ports).stop(self.value(False))
        self.assertEqual(ports.calls, ["fence", "stop-delegated", "stop-migrated"])
        self.assertTrue(result["remote_confirmed"])
        ports = ControlPorts()
        ports.project_failure = True
        with self.assertRaises(OSError):
            CollectionControl(ports).stop(self.value(False))
        self.assertTrue(ports.fenced)
        self.assertEqual(ports.calls, ["fence", "stop-delegated"])

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
