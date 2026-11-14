"""Synthetic same-account reconnection and non-secret mismatch diagnostics."""

import copy
import unittest

from agent_brain_client.application.installation.fresh import (
    FreshConnectionMismatch,
    validate_reconnection,
)


class ReconnectionAuthorization(unittest.TestCase):
    def setUp(self):
        ids = {kind: "personal-owner" for kind in ("memory", "rule", "skill")}
        self.expected = {
            "endpoint": "https://synthetic.invalid",
            "grant": {"actor": "synthetic-account", "scopes": ["read", "write"],
                      "artifact_types": list(ids), "ids": ids},
            "content": {"schema": 1, "adapter": "codex", "kinds": list(ids),
                        "ids": ids, "dependencies": {"commands": [], "python_modules": []}},
        }
        self.connection = {key: copy.deepcopy(self.expected[key]) for key in ("endpoint", "grant")}

    def test_write_and_put_are_equivalent_in_both_directions(self):
        self.connection["grant"]["scopes"] = ["read", "put"]
        validate_reconnection(self.expected, self.connection)
        self.expected["grant"]["scopes"] = ["read", "put"]
        self.connection["grant"]["scopes"] = ["read", "write"]
        validate_reconnection(self.expected, self.connection)

    def test_real_permission_loss_still_blocks(self):
        self.connection["grant"]["scopes"] = ["read"]
        with self.assertRaises(FreshConnectionMismatch) as caught:
            validate_reconnection(self.expected, self.connection)
        self.assertEqual(caught.exception.reasons, ["capability-scopes"])

    def test_each_identity_and_content_dimension_is_reported_without_values(self):
        self.connection["endpoint"] = "https://another.invalid"
        self.connection["grant"].update(actor="OTHER_PRIVATE_ACTOR", scopes=["put"],
                                        artifact_types=["memory"], ids={"memory": []})
        with self.assertRaises(FreshConnectionMismatch) as caught:
            validate_reconnection(self.expected, self.connection)
        self.assertEqual(caught.exception.reasons, ["server-origin", "account-actor",
            "capability-scopes", "content-read", "content-kinds", "content-ids"])
        self.assertNotIn("OTHER_PRIVATE_ACTOR", str(caught.exception))
        self.assertNotIn("another.invalid", str(caught.exception))

    def test_invalid_retained_policy_is_not_wrong_account(self):
        self.expected["content"]["schema"] = 999
        with self.assertRaises(Exception) as caught:
            validate_reconnection(self.expected, self.connection)
        self.assertNotIsInstance(caught.exception, FreshConnectionMismatch)
