"""Additive capture diagnostics never replace the committed-event proof."""

import copy
import unittest
from unittest.mock import Mock, patch

from agent_brain_client.adapters.server.collection import CollectionClient
from agent_brain_client.domain.receipts import core_receipt
from test_event_hook import accepted


class CaptureReceiptCompatibility(unittest.TestCase):
    def setUp(self):
        event = {
            "contract": "agentbrain.event.v1",
            "event_id": "synthetic-event",
            "project_id": "synthetic-project",
            "session_id": "synthetic-session",
            "occurred_at": "2026-10-17T12:00:00.000Z",
            "messages": [{"role": "user", "content": "Synthetic test fact"}],
        }
        self.request = {
            "request_id": "synthetic-request-001",
            "method": "memory_event_submit",
            "params": {"kind": "memory", "id": event["event_id"], "event": event},
        }
        self.diagnostic = {
            "state": "pending",
            "reason": "capture_pending",
            "model_output_truth_verified": False,
        }

    def test_old_new_and_replayed_receipts(self):
        for diagnostic in (None, self.diagnostic):
            for replayed in (None, False, True):
                with self.subTest(diagnostic=diagnostic, replayed=replayed):
                    response = accepted(self.request)
                    if diagnostic is not None:
                        response["result"]["capture_extraction"] = diagnostic
                    if replayed is not None:
                        response["result"]["replayed"] = replayed
                    before = copy.deepcopy(response)
                    self.assertTrue(core_receipt(response, self.request))
                    self.assertEqual(response, before)

    def test_extension_cannot_repair_invalid_base_receipt(self):
        response = accepted(self.request)
        response["result"]["capture_extraction"] = self.diagnostic
        cases = [
            ("request_id", "wrong"),
            ("result.event_id", "wrong"),
            ("result.message_ids", []),
            ("result.model_extraction_verified", True),
            ("result.replayed", 1),
            ("result.unknown", {}),
            ("persistence.durable", True),
            ("persistence.github_confirmed", True),
            ("persistence.receipt_ledger_durable", 1),
        ]
        for path, value in cases:
            with self.subTest(path=path):
                candidate = copy.deepcopy(response)
                keys = path.split(".")
                target = candidate if len(keys) == 1 else candidate[keys[0]]
                target[keys[-1]] = value
                self.assertFalse(core_receipt(candidate, self.request))
        for invalid in (None, [], "pending", True, 1):
            with self.subTest(invalid=invalid):
                candidate = copy.deepcopy(response)
                candidate["result"]["capture_extraction"] = invalid
                self.assertFalse(core_receipt(candidate, self.request))

    def test_collection_transport_preserves_diagnostic_and_exact_request(self):
        client = object.__new__(CollectionClient)
        client.value = {"scope": {}}
        client.local = {"kind": "project"}
        client.actor = "synthetic-owner"
        client.params = Mock(return_value={"origin": client.local, "session_id": "synthetic-session"})
        response = accepted(self.request)
        response["result"]["capture_extraction"] = self.diagnostic
        client.wire = Mock(return_value=response)
        with (
            patch("agent_brain_client.adapters.server.collection.select", return_value={"sharing": "personal"}),
            patch("agent_brain_client.adapters.server.collection.source_id", return_value="synthetic-source"),
            patch("agent_brain_client.adapters.server.collection.namespace", return_value="synthetic-project"),
        ):
            self.assertEqual(client.request(self.request), response)
            client.wire.assert_called_once_with(
                "memory_global_event_submit",
                {"origin": client.local, "session_id": "synthetic-session", "event": self.request["params"]["event"]},
                self.request["request_id"],
            )
            response["result"]["event_id"] = "wrong"
            with self.assertRaisesRegex(ValueError, "Unconfirmed global Core receipt"):
                client.request(self.request)


if __name__ == "__main__":
    unittest.main()
