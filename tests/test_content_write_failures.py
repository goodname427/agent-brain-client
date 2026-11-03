"""Synthetic Mac write failures; no device credentials, real memories or network."""

from contextlib import contextmanager, redirect_stderr
import io
import json
import unittest
from unittest.mock import patch

from brain_lock import locked
from content_mcp import ContentParameterError, ContentServer
from remote_http import RequestStopped
import test_content_outbox as fixture


@contextmanager
def short_lock(path):
    with locked(path, timeout=0.01) as handle:
        yield handle


class WriteFailures(unittest.TestCase):
    def setUp(self):
        self.f = fixture.Outbox(
            "test_write_saved_and_fsynced_before_network_and_restart_reconnect"
        )
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.server = ContentServer(self.f.adapter)
        self.request = self.f.request()

    def invoke(self):
        return self.server.invoke(
            "content_put",
            dict(self.request["params"], request_id=self.request["request_id"]),
        )["structuredContent"]

    def test_real_content_and_enqueue_lock_contention_is_retryable_and_saves_once(self):
        for filename, stage in [
            ("content.lock", "content-lock"),
            ("outbox-enqueue.lock", "outbox-lock"),
        ]:
            with self.subTest(lock=filename):
                with (
                    locked(self.f.cache / filename),
                    patch("content_cache.locked", short_lock),
                ):
                    result = self.invoke()
                self.assertEqual(
                    result["error"],
                    {
                        "code": "content_busy",
                        "retryable": True,
                        "stage": stage,
                        "exception_type": "LockTimeout",
                    },
                )
                self.assertEqual(result["request_id"], self.request["request_id"])
                self.assertFalse(result["persistence"]["durable"])
                self.assertEqual(self.f.cloud.calls, [])
                self.assertEqual(self.f.adapter.queue_status()["pending"], 0)
        result = self.invoke()  # Only this explicit identical retry sends the write.
        self.assertEqual(result["local_queue"]["state"], "confirmed")
        self.assertEqual(self.f.cloud.puts, 1)
        self.assertEqual(self.invoke()["result"], result["result"])
        self.assertEqual(self.f.cloud.puts, 1)

    def test_enqueue_fsync_failure_has_stage_and_type_without_secret_detail(self):
        stderr = io.StringIO()
        with (
            patch(
                "content_cache.os.fsync",
                side_effect=OSError("SYNTHETIC_PRIVATE_PATH_TOKEN_BODY"),
            ),
            redirect_stderr(stderr),
        ):
            result = self.invoke()
        self.assertEqual(
            result["error"],
            {
                "code": "content_adapter_blocked",
                "retryable": False,
                "stage": "outbox-enqueue",
                "exception_type": "OSError",
            },
        )
        self.assertEqual(self.f.cloud.calls, [])
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result) + stderr.getvalue())
        with patch.object(
            self.f.adapter, "invoke", side_effect=ValueError("SYNTHETIC_PRIVATE")
        ):
            self.assertEqual(self.invoke()["error"]["stage"], "adapter-invoke")

    def test_lost_local_confirmation_reports_its_stage_and_exact_retry_is_idempotent(
        self,
    ):
        save = self.f.adapter._save

        def fail_confirmation(name, value):
            if name.startswith("outbox/") and value.get("state") == "confirmed":
                raise OSError("SYNTHETIC_PRIVATE_PATH_TOKEN_BODY")
            return save(name, value)

        with patch.object(self.f.adapter, "_save", side_effect=fail_confirmation):
            result = self.invoke()
        self.assertEqual(result["error"]["stage"], "outbox-confirmation")
        self.assertEqual(result["error"]["exception_type"], "OSError")
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))
        self.assertEqual(self.f.cloud.puts, 1)
        self.assertEqual(self.f.adapter.queue_status()["pending"], 1)
        self.assertEqual(self.invoke()["local_queue"]["state"], "confirmed")
        self.assertEqual(self.f.cloud.puts, 1)

    def test_healthy_32_entry_queue_does_not_block_new_write(self):
        for index in range(32):
            request = self.f.request(f"synthetic_previous_{index:04d}")
            request["params"]["id"] = f"synthetic-topic-{index}"
            self.f.adapter.invoke(request)
        self.assertEqual(self.f.adapter.queue_status()["confirmed"], 32)
        self.assertEqual(self.invoke()["local_queue"]["state"], "confirmed")
        self.assertEqual(self.f.cloud.puts, 33)

    def test_failed_error_response_save_is_not_a_server_confirmation(self):
        original = self.f.cloud.request
        save = self.f.adapter._save
        for code in ("invalid_request", "revision_conflict"):
            with self.subTest(code=code):

                def deny(request):
                    if request["method"] == "content_put":
                        return {
                            "request_id": request["request_id"],
                            "error": {"code": code, "retryable": False},
                            "persistence": {"durable": False},
                        }
                    return original(request)

                def fail_response(name, value):
                    if name.startswith("outbox/") and value.get("state") in (
                        "blocked",
                        "conflict",
                    ):
                        raise OSError("SYNTHETIC_PRIVATE_PATH_TOKEN_BODY")
                    return save(name, value)

                with (
                    patch.object(self.f.cloud, "request", side_effect=deny),
                    patch.object(self.f.adapter, "_save", side_effect=fail_response),
                ):
                    result = self.invoke()
                self.assertEqual(result["error"]["stage"], "outbox-response")
                self.assertEqual(result["error"]["exception_type"], "OSError")
                self.assertFalse(result["persistence"]["durable"])
                self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))
                self.assertEqual(self.f.cloud.puts, 0)
                self.assertEqual(self.f.adapter.queue_status()["pending"], 1)

    def test_unknown_exception_name_and_message_are_not_exposed_or_reclassified_as_parameters(
        self,
    ):
        private_error = type("SYNTHETIC_PRIVATE_TYPE", (Exception,), {})
        with patch.object(
            self.f.adapter,
            "invoke",
            side_effect=private_error("SYNTHETIC_PRIVATE_BODY"),
        ):
            result = self.invoke()
        self.assertEqual(result["error"]["stage"], "adapter-invoke")
        self.assertEqual(result["error"]["exception_type"], "Exception")
        self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))
        with self.assertRaises(ContentParameterError):
            self.server.invoke("content_put", {})
        with patch.object(
            self.f.adapter, "invoke", side_effect=RequestStopped("request_cancelled")
        ):
            with self.assertRaises(RequestStopped):
                self.invoke()
        # Diagnostics from a previous failed call must not bleed into this one.
        with patch.object(
            self.f.adapter, "invoke", side_effect=ValueError("SYNTHETIC_PRIVATE")
        ):
            self.assertEqual(self.invoke()["error"]["stage"], "adapter-invoke")


if __name__ == "__main__":
    unittest.main()
