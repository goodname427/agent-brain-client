"""Query bypasses unrelated blocked mapping; no online-time claims offline."""

import unittest
from unittest.mock import patch
import test_content_outbox as fixture
from content_mcp import ContentServer, ProtocolError


class MemoryQueryClient(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.Outbox(
            "test_write_saved_and_fsynced_before_network_and_restart_reconnect"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def reply(self, request):
        return {
            "request_id": request["request_id"],
            "result": {
                "content_api_version": 1,
                "commit": "a" * 40,
                "method": "recent-updates",
                "time_basis": "memory_updated_at",
                "topics": [
                    {
                        "id": "synthetic-topic",
                        "title": "Synthetic progress",
                        "description": "Useful small fact",
                        "revision": "b" * 64,
                        "updated_at": "2026-10-12T12:00:00Z",
                        "score": 0,
                        "preview": "A small useful update",
                    }
                ],
            },
            "persistence": {"durable": False, "state": "read-only"},
        }

    def test_mcp_defaults_and_query_does_not_refresh_or_touch_queue(self):
        adapter = self.fixture.adapter
        self.fixture.cloud.request = self.reply
        with patch.object(
            adapter,
            "refresh",
            side_effect=AssertionError(
                "Unrelated blocked Skill refresh must not gate query"
            ),
        ):
            response = ContentServer(adapter).invoke("memory_query", {})
        self.assertFalse(response["isError"], response)
        self.assertEqual(
            response["structuredContent"]["result"]["topics"][0]["preview"],
            "A small useful update",
        )
        self.assertFalse((self.fixture.cache / "outbox").exists())
        with patch.object(
            adapter, "refresh", side_effect=AssertionError("Query alias is read-only")
        ):
            alias = ContentServer(adapter).invoke(
                "content_catalog", {"kinds": ["memory"], "query": ""}
            )
        self.assertFalse(alias["isError"], alias)
        with self.assertRaises(ProtocolError):
            ContentServer(adapter).invoke(
                "content_catalog", {"kinds": ["rule"], "query": ""}
            )

    def test_wrong_scope_or_response_is_rejected_and_offline_not_fresh(self):
        adapter = self.fixture.adapter
        self.fixture.cloud.request = lambda request: dict(
            self.reply(request),
            result=dict(
                self.reply(request)["result"],
                topics=[
                    dict(self.reply(request)["result"]["topics"][0], id="not-allowed")
                ],
            ),
        )
        with patch.dict(adapter.policy["ids"], memory=["synthetic-topic"]):
            self.assertTrue(
                ContentServer(adapter).invoke("memory_query", {})["isError"]
            )

        def disconnected(request):
            raise OSError("Synthetic disconnected")

        self.fixture.cloud.request = disconnected
        response = ContentServer(adapter).invoke("memory_query", {})
        self.assertTrue(response["isError"])
        self.assertEqual(
            response["structuredContent"]["error"]["code"], "memory_query_unavailable"
        )


if __name__ == "__main__":
    unittest.main()
