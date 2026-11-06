"""Read-only lifecycle gates delegate journal interpretation to their owners."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest
from agent_brain_client.adapters.harnesses.codex.sources import CodexSourceImports
from agent_brain_client.adapters.harnesses.codex.mapping import CodexContentMapping
import test_content_offline_reads as offline_fixture
import test_client_uninstall as lifecycle_fixture


class OwnerGates(unittest.TestCase):
    def test_source_removal_inventory_is_read_only_without_transport_identity(self):
        for phase in (
            "inventoried",
            "pending",
            "conflict",
            "blocked",
            "imported",
            "duplicate",
            "reconciled",
            "unknown",
        ):
            state = {
                "schema": 1,
                "policy_sha": "unobserved-without-credentials",
                "items": {"synthetic": {"status": phase}},
                "coverage": [],
            }
            before = deepcopy(state)
            session = SimpleNamespace(_load=Mock(return_value=state))
            owner = CodexSourceImports(session)  # No queue_sha, credential or writer.
            result = owner.removal_blocker()
            self.assertEqual(
                result is None, phase in ("imported", "duplicate", "reconciled")
            )
            self.assertEqual(state, before)
        for state, expected in (
            (None, None),
            ({}, "Import inventory retained"),
            (
                {"schema": 1, "policy_sha": "x", "items": {"x": None}, "coverage": []},
                "Unknown import retained",
            ),
        ):
            self.assertEqual(
                CodexSourceImports(
                    SimpleNamespace(_load=lambda *a: state)
                ).removal_blocker(),
                expected,
            )

    def test_mapping_gate_preserves_read_only_settlement_semantics(self):
        for journal, settled in (
            (None, True),
            ({}, True),
            ({"phase": "complete"}, True),
            ({"phase": "rolled-back"}, True),
            ({"phase": "applying"}, False),
            ({"phase": "unknown"}, False),
        ):
            before = deepcopy(journal)
            owner = CodexContentMapping(SimpleNamespace(_load=lambda *a: journal))
            self.assertEqual(owner.transaction_settled(), settled)
            self.assertEqual(journal, before)

    def test_offline_reader_defers_to_mapping_gate_without_loading_journal(self):
        fixture = offline_fixture.OfflineReads()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.warm()
        fixture.cloud.offline = True
        original = fixture.adapter._load

        def read(name, *args):
            self.assertNotEqual(name, "transaction.json")
            return original(name, *args)

        with (
            patch.object(
                fixture.adapter.mapping, "transaction_settled", return_value=False
            ) as gate,
            patch.object(fixture.adapter, "_load", side_effect=read),
        ):
            result = fixture.adapter.invoke(fixture.request())
        self.assertEqual(result["error"]["code"], "content_offline_cache_unverified")
        gate.assert_called_once_with()

    def test_uninstall_uses_source_and_mapping_owner_gates_without_credentials(self):
        fixture = lifecycle_fixture.ClientUninstall(
            "test_cancel_is_read_only_and_status_never_reads_credentials"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        from agent_brain_client.adapters.harnesses.codex import lifecycle

        with (
            patch.object(
                fixture.f.backend,
                "load",
                side_effect=AssertionError("No credential access"),
            ),
            patch.object(
                CodexSourceImports,
                "removal_blocker",
                return_value="Import inventory retained",
            ) as source,
            patch.object(CodexContentMapping, "transaction_settled") as mapping,
        ):
            with self.assertRaisesRegex(
                lifecycle.install.InstallBlocked, "Import inventory retained"
            ):
                lifecycle.prepare_removal(fixture.home)
            source.assert_called_once_with()
            mapping.assert_not_called()
        with (
            patch.object(
                CodexSourceImports, "removal_blocker", return_value=None
            ) as source,
            patch.object(
                CodexContentMapping, "transaction_settled", return_value=False
            ) as mapping,
        ):
            with self.assertRaisesRegex(
                lifecycle.install.InstallBlocked, "Mapping transaction needs repair"
            ):
                lifecycle.prepare_removal(fixture.home)
            source.assert_called_once_with()
            mapping.assert_called_once_with()
