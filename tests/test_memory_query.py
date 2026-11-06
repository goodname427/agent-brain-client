"""One query entry and legacy aliases share the approved native read path."""

import unittest
from unittest.mock import patch
from agent_brain_client.interfaces.mcp.content import ContentServer, ProtocolError
from test_windows_acceptance_repairs import WindowsAcceptanceRepairs


class MemoryQueryClient(unittest.TestCase):
    def setUp(self):
        self.f = WindowsAcceptanceRepairs()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_query_alias_and_defaults_do_not_refresh_or_replay(self):
        adapter = self.f.adapter
        server = ContentServer(adapter)
        with (
            patch.object(adapter, "refresh", side_effect=AssertionError("No refresh")),
            patch.object(adapter, "_drain", side_effect=AssertionError("No replay")),
        ):
            current = server.invoke("memory_query", self.f.params)
            legacy = server.invoke("memory_recall", self.f.params)
            self.assertFalse(current["isError"])
            self.assertFalse(legacy["isError"])
            self.assertEqual(
                current["structuredContent"]["result"],
                legacy["structuredContent"]["result"],
            )
            with patch("agent_brain_client.adapters.storage.origins.discover", return_value=self.f.local):
                default = server.invoke("memory_query", {"query": "Synthetic progress"})
            self.assertFalse(default["isError"])
            with patch("agent_brain_client.adapters.storage.origins.discover", return_value=self.f.local):
                alias = server.invoke(
                    "content_catalog",
                    {"kinds": ["memory"], "query": "Synthetic progress"},
                )
            self.assertFalse(alias["isError"])
        self.assertFalse((self.f.fixture.cache / "outbox").exists())
        with self.assertRaises(ProtocolError):
            server.invoke(
                "memory_query", dict(self.f.params, since="2026-01-01T00:00:00Z")
            )

    def test_native_options_and_count_are_not_clamped(self):
        observed=[]
        original=self.f.client.transport.request
        def request(value):
            observed.append(value)
            return original(value)
        self.f.client.transport.request=request
        server=ContentServer(self.f.adapter)
        result=server.invoke('memory_query',dict(self.f.params,limit=8,recall={'scoreThreshold':0.2,'maxCharsPerMemory':0,'maxTotalRecallChars':1000}))
        self.assertFalse(result['isError'])
        self.assertEqual(observed[-1]['params']['limit'],8)
        self.assertEqual(observed[-1]['params']['recall']['scoreThreshold'],0.2)
        for options in ({'scoreThreshold':float('nan')},{'maxTotalRecallChars':-1},{'provider':'new-endpoint'}):
            with self.assertRaises(ProtocolError):
                server.invoke('memory_query',dict(self.f.params,recall=options))

    def test_transport_failure_is_unconfirmed_without_lexical_fallback(self):
        def unavailable(request):
            raise OSError("Synthetic offline")

        self.f.client.transport.request = unavailable
        result = ContentServer(self.f.adapter).invoke("memory_query", self.f.params)
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error"]["code"], "layered_recall_unconfirmed"
        )

    def test_native_l1_save_receipt_does_not_require_a_fabricated_git_commit(self):
        f = self.f.fixture
        request = {
            "request_id": "fictional_native_save_0001",
            "method": "content_put",
            "params": {
                "kind": "memory",
                "id": "synthetic-topic",
                "record": {
                    "title": "Fictional",
                    "description": "Direct L1",
                    "body": "Fictional fact",
                    "type": "user",
                },
                "expected_revision": "missing",
            },
        }
        params = request["params"]
        response = {
            "request_id": request["request_id"],
            "result": {
                "content_api_version": 1,
                "kind": "memory",
                "id": params["id"],
                "revision": "b" * 64,
                "record": params["record"],
            },
            "persistence": {
                "storage": "tencent-l1",
                "checkpoint": "c" * 64,
                "state": "local-confirmed",
                "durable": False,
                "local_durable": True,
                "github_confirmed": False,
            },
        }
        self.assertTrue(f.adapter._server_saved(response, request))
        response["persistence"]["checkpoint"] = "invalid"
        self.assertFalse(f.adapter._server_saved(response, request))


if __name__ == "__main__":
    unittest.main()
