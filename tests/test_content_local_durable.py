"""Client acknowledges Server local durability without claiming GitHub backup."""

import copy
import unittest

import test_content_outbox as fixture
from content_cache import ContentAdapter


class LocalDurable(unittest.TestCase):
    def setUp(self):
        self.f = fixture.Outbox(
            "test_write_saved_and_fsynced_before_network_and_restart_reconnect"
        )
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.github = False
        self.original = self.f.cloud.request
        self.f.cloud.request = self.request

    def request(self, request):
        response = self.original(request)
        if request["method"] == "content_put" and "error" not in response:
            response = copy.deepcopy(response)
            response["persistence"].update(
                durable=self.github,
                local_durable=True,
                github_confirmed=self.github,
                state="remote-confirmed" if self.github else "local-confirmed",
            )
        elif (
            request["method"] in ("content_catalog", "content_read")
            and "error" not in response
        ):
            response = dict(
                response,
                persistence={
                    "state": "read-only",
                    "durable": False,
                    "source": "server-local",
                    "github_backup": {
                        "state": "confirmed" if self.github else "pending"
                    },
                },
            )
        return response

    def test_local_server_ack_allows_next_cas_and_mapping_without_false_backup(self):
        one = self.f.adapter.invoke(self.f.request())
        self.assertFalse(one["persistence"]["durable"])
        self.assertTrue(one["persistence"]["local_durable"])
        self.assertFalse(one["local_queue"]["github_confirmed"])
        self.assertTrue(one["local_queue"]["server_saved"])
        self.assertEqual(self.f.adapter.queue_status()["pending"], 0)
        self.assertEqual(one["local_refresh"]["status"], "content-refreshed")
        two = self.f.adapter.invoke(
            self.f.request(
                "synthetic_local_second_001",
                body="Synthetic next write",
                revision=one["result"]["revision"],
            )
        )
        self.assertTrue(two["persistence"]["local_durable"])
        self.assertFalse(two["persistence"]["durable"])
        self.assertEqual(self.f.cloud.puts, 2)
        read = self.f.adapter.invoke(
            {
                "request_id": "synthetic_local_read_001",
                "method": "content_read",
                "params": {"kind": "memory", "id": "synthetic-topic"},
            }
        )
        self.assertEqual(read["result"]["record"]["body"], "Synthetic next write")
        self.assertEqual(read["persistence"]["github_backup"]["state"], "pending")

    def test_restart_does_not_poll_confirmed_rows_and_same_id_upgrades_receipt(self):
        request = self.f.request()
        one = self.f.adapter.invoke(request)
        before = self.f.cloud.puts
        calls = len(self.f.cloud.calls)
        restarted = ContentAdapter(
            self.f.home,
            self.f.cache,
            fixture.POLICY,
            self.f.cloud,
            codex_home=self.f.home / ".codex",
        )
        restarted.refresh()
        self.assertEqual(self.f.cloud.puts, before)
        self.assertFalse(
            any(r["method"] == "content_put" for r in self.f.cloud.calls[calls:])
        )
        self.github = True
        upgraded = restarted.invoke(request)
        self.assertTrue(upgraded["persistence"]["github_confirmed"])
        self.assertTrue(upgraded["persistence"]["durable"])
        self.assertEqual(upgraded["result"], one["result"])
        self.assertEqual(self.f.cloud.puts, before)

    def test_missing_or_contradictory_local_receipt_does_not_acknowledge(self):
        original = self.f.cloud.request

        def corrupt(request):
            response = original(request)
            if request["method"] == "content_put":
                response = copy.deepcopy(response)
                response["persistence"]["github_confirmed"] = True
            return response

        self.f.cloud.request = corrupt
        result = self.f.adapter.invoke(self.f.request())
        self.assertEqual(result["local_queue"]["state"], "pending")
        self.assertFalse(result["local_queue"]["server_saved"])
        self.assertEqual(self.f.adapter.queue_status()["pending"], 1)


if __name__ == "__main__":
    unittest.main()
