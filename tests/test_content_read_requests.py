"""Isolated live reads and MCP queue cancellation; all content is synthetic."""

import copy
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import build_opener, ProxyHandler

import test_content_outbox as fixtures
import content_contract as contract
from content_mcp import ContentServer, serve
from remote_http import (
    RemoteClient,
    RequestControl,
    RequestStopped,
    controlled_run,
    NoRedirect,
    ReadHTTPHandler,
    ReadHTTPSHandler,
)

MEMORY = fixtures.MEMORY


def call(identifier, name="content_read", arguments=None):
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": arguments
            or {"kind": "memory", "id": "synthetic-" + str(identifier)},
        },
    }


class Source:
    def __init__(self):
        self.lines = queue.Queue()

    def send(self, value):
        self.lines.put(json.dumps(value).encode() + b"\n")

    def readline(self, size):
        return self.lines.get()

    def end(self):
        self.lines.put(b"")


class Output:
    def __init__(self):
        self.responses = queue.Queue()

    def write(self, value):
        self.responses.put(json.loads(value))

    def flush(self):
        pass

    def get(self):
        return self.responses.get(timeout=3)


class ReadRequests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Outbox("test_same_id_cannot_change_request_or_cas_base")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.cloud.records = {
            ("memory", "synthetic-" + str(i)): copy.deepcopy(MEMORY)
            for i in range(1, 24)
        }
        self.server = ContentServer(self.f.adapter)
        self.server.state = "ready"

    def start(self, server=None, **options):
        source = Source()
        output = Output()
        thread = threading.Thread(
            target=serve, args=(server or self.server, source, output), kwargs=options
        )
        thread.start()

        def close():
            source.end()
            thread.join(4)
            self.assertFalse(thread.is_alive(), "Synthetic MCP executor did not exit")

        self.addCleanup(close)
        return source, output

    def test_23_concurrent_reads_fetch_each_record_once_and_full_catalog_once(self):
        source, output = self.start(request_timeout=2)
        source.send(call(24, "content_catalog", {"kinds": ["memory"]}))
        for i in range(1, 24):
            source.send(call(i))
        replies = [output.get() for _ in range(24)]
        catalog = next(r for r in replies if r["id"] == 24)["result"][
            "structuredContent"
        ]
        self.assertEqual(len(catalog["result"]["artifacts"]), 23)
        self.assertEqual({r["id"] for r in replies}, set(range(1, 25)))
        self.assertTrue(all(not r["result"]["isError"] for r in replies))
        self.assertEqual(
            [r["method"] for r in self.f.cloud.calls].count("content_catalog"), 1
        )
        self.assertEqual(
            [r["method"] for r in self.f.cloud.calls].count("content_read"), 23
        )
        self.assertFalse((self.f.home / ".config/agent-brain-client/content").exists())

    def test_live_read_observes_remote_revision_and_reports_mapping_deferred(self):
        self.f.adapter.refresh()
        self.f.cloud.calls.clear()
        before = (
            self.f.home / ".config/agent-brain-client/content/memory/synthetic-1.md"
        ).read_bytes()
        changed = dict(MEMORY, body="Synthetic newer Server content")
        self.f.cloud.records[("memory", "synthetic-1")] = changed
        reply = self.server.invoke(
            "content_read", {"kind": "memory", "id": "synthetic-1"}
        )["structuredContent"]
        self.assertEqual(
            reply["result"], contract.envelope("memory", "synthetic-1", changed)
        )
        self.assertNotIn("local_cache", reply)
        self.assertEqual(reply["local_refresh"]["status"], "read-only")
        self.assertEqual(len(self.f.cloud.calls), 1)
        self.assertEqual(
            (
                self.f.home / ".config/agent-brain-client/content/memory/synthetic-1.md"
            ).read_bytes(),
            before,
        )
        self.assertEqual(self.f.adapter.refresh()["status"], "content-refreshed")

    def test_catalog_duplicates_oversize_bad_revision_and_scope_fail_visibly(self):
        original = self.f.cloud.request
        for change in ("duplicate", "oversize", "revision", "scope"):

            def malformed(request):
                response = original(request)
                rows = response["result"]["artifacts"]
                if change == "duplicate":
                    rows.append(copy.deepcopy(rows[0]))
                elif change == "oversize":
                    response["result"]["artifacts"] = rows * 12
                elif change == "revision":
                    rows[0]["revision"] = "invalid"
                else:
                    rows[0]["kind"] = "rule"
                return response

            self.f.cloud.request = malformed
            self.assertTrue(
                self.server.invoke("content_catalog", {"kinds": ["memory"]})["isError"]
            )
        self.f.cloud.request = original
        self.assertFalse(
            self.server.invoke("content_catalog", {"kinds": ["memory"]})["isError"]
        )

    def test_invalid_typed_live_body_is_never_replaced_with_cached_success(self):
        self.f.adapter.refresh()
        original = self.f.cloud.request

        def invalid(request):
            response = original(request)
            response["result"]["record"]["type"] = "unapproved"
            return response

        self.f.cloud.request = invalid
        reply = self.server.invoke(
            "content_read", {"kind": "memory", "id": "synthetic-1"}
        )
        self.assertTrue(reply["isError"])
        self.assertNotIn("local_cache", reply["structuredContent"])

    def test_queue_deadlines_skip_23_expired_reads_and_next_request_progresses(self):
        started = threading.Event()
        release = threading.Event()
        original = self.f.cloud.request

        def slow_write(request):
            if request["method"] == "content_put":
                started.set()
                release.wait(2)
            return original(request)

        self.f.cloud.request = slow_write
        self.addCleanup(release.set)
        source, output = self.start(request_timeout=0.15)
        write = self.f.request()
        source.send(
            call(
                100,
                "content_put",
                dict(write["params"], request_id=write["request_id"]),
            )
        )
        self.assertTrue(started.wait(1))
        for i in range(1, 24):
            source.send(call(i))
        time.sleep(0.25)
        release.set()
        replies = [output.get() for _ in range(24)]
        self.assertEqual(
            sum(r.get("error", {}).get("code") == -32001 for r in replies), 23
        )
        # The durable write and its one refresh remain recoverable and unchanged.
        self.assertEqual(self.f.cloud.puts, 1)
        self.assertEqual(self.f.adapter.queue_status()["confirmed"], 1)
        self.f.cloud.calls.clear()
        source.send(call(200, arguments={"kind": "memory", "id": "synthetic-1"}))
        self.assertFalse(output.get()["result"]["isError"])
        self.assertEqual([r["method"] for r in self.f.cloud.calls], ["content_read"])

    def test_queued_cancel_does_not_start_write_and_active_write_keeps_receipt(self):
        started = threading.Event()
        release = threading.Event()
        original = self.f.cloud.request

        def slow_write(request):
            if request["method"] == "content_put":
                started.set()
                release.wait(2)
            return original(request)

        self.f.cloud.request = slow_write
        self.addCleanup(release.set)
        source, output = self.start()
        write = self.f.request()
        source.send(
            call(
                100,
                "content_put",
                dict(write["params"], request_id=write["request_id"]),
            )
        )
        self.assertTrue(started.wait(1))
        second = self.f.request("synthetic_cancel_queued_write")
        source.send(
            call(
                101,
                "content_put",
                dict(second["params"], request_id=second["request_id"]),
            )
        )
        for i in (100, 101):
            source.send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": i},
                }
            )
        # Wait until the receiver has consumed the cancellation notification.
        time.sleep(0.04)
        release.set()
        replies = {r["id"]: r for r in (output.get(), output.get())}
        self.assertEqual(replies[101]["error"]["code"], -32800)
        self.assertTrue(
            replies[100]["result"]["structuredContent"]["persistence"]["durable"]
        )
        self.assertFalse(
            (self.f.cache / "outbox" / ("synthetic_cancel_queued_write.json")).exists()
        )
        self.assertEqual(self.f.cloud.puts, 1)

    def test_closed_output_exits_without_starting_more_requests(self):
        class ClosedOutput:
            def write(self, value):
                raise BrokenPipeError("Synthetic disconnected host")

        source = io.BytesIO(
            b"".join(json.dumps(call(i)).encode() + b"\n" for i in range(1, 24))
        )
        serve(self.server, source, ClosedOutput())
        self.assertEqual(len(self.f.cloud.calls), 1)
        self.assertEqual(self.f.cloud.puts, 0)

    def test_offline_read_cancel_releases_waiting_lock_without_touching_snapshot(self):
        from brain_lock import locked

        self.f.adapter.refresh()
        self.f.cloud.offline = True
        before = (self.f.cache / "active.json").read_bytes()
        control = RequestControl(2)
        errors = []

        def read():
            try:
                with control.activate():
                    self.server.invoke(
                        "content_read", {"kind": "memory", "id": "synthetic-1"}
                    )
            except RequestStopped as error:
                errors.append(error.reason)
            finally:
                control.close()

        with locked(self.f.cache / "content.lock"):
            worker = threading.Thread(target=read)
            worker.start()
            time.sleep(0.05)
            control.cancel()
            worker.join(0.5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ["request_cancelled"])
        self.assertEqual((self.f.cache / "active.json").read_bytes(), before)

    def test_cancel_before_write_start_and_after_read_result_is_not_lost(self):
        for reason in ("request_cancelled", "request_deadline_exceeded"):
            control = RequestControl(2)
            control.cancel(reason)
            with self.assertRaises(RequestStopped):
                control.close(check=True)
            control.close()


class HTTPRequests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token = Path(self.tmp.name) / "synthetic.token"
        self.token.write_text("SYNTHETIC_ONLY_TOKEN_FIXTURE_1234567890")
        self.token.chmod(0o600)
        self.started = threading.Event()
        self.release = threading.Event()
        self.seen = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                identity = request["params"]["id"]
                fixture.seen.append(identity)
                raw = json.dumps(
                    {
                        "request_id": request["request_id"],
                        "result": contract.envelope("memory", identity, MEMORY),
                        "persistence": {"durable": False},
                    }
                ).encode()
                try:
                    if identity == "synthetic-1":
                        # Block before headers, not just between body chunks.
                        fixture.started.set()
                        fixture.release.wait(3)
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    if identity == "synthetic-2":
                        self.wfile.write(raw[:1])
                        self.wfile.flush()
                        fixture.started.set()
                        fixture.release.wait(3)
                        self.wfile.write(raw[1:])
                    else:
                        self.wfile.write(raw)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()

        def close():
            self.release.set()
            self.http.shutdown()
            self.http.server_close()
            self.http_thread.join(2)

        self.addCleanup(close)
        self.client = RemoteClient(
            "http://127.0.0.1:" + str(self.http.server_port), self.token
        )
        client = self.client

        class Adapter:
            def methods(self):
                return {"content_read"}

            def invoke(self, request):
                return client.request(request)

        self.server = ContentServer(Adapter())
        self.server.state = "ready"

    def start(self, **options):
        source = Source()
        output = Output()
        thread = threading.Thread(
            target=serve, args=(self.server, source, output), kwargs=options
        )
        thread.start()

        def close():
            self.release.set()
            source.end()
            thread.join(3)
            self.assertFalse(thread.is_alive())

        self.addCleanup(close)
        return source, output

    def test_cancel_active_http_and_queued_reads_without_blocking_next_request(self):
        source, output = self.start()
        source.send(call(1))
        self.assertTrue(self.started.wait(1))
        for i in range(3, 24):
            source.send(call(i))
        for i in range(3, 24):
            source.send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": i},
                }
            )
        source.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 1},
            }
        )
        start = time.monotonic()
        source.send(call(24))
        replies = {r["id"]: r for r in [output.get() for _ in range(23)]}
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(
            sum(r.get("error", {}).get("code") == -32800 for r in replies.values()), 22
        )
        self.assertFalse(replies[24]["result"]["isError"])
        self.assertEqual(self.seen, ["synthetic-1", "synthetic-24"])

    def test_total_deadline_interrupts_slow_body_and_eof_finishes_cleanly(self):
        source, output = self.start(request_timeout=0.15)
        source.send(call(2))
        self.assertTrue(self.started.wait(1))
        started = time.monotonic()
        response = output.get()
        self.assertEqual(response["error"]["code"], -32001)
        self.assertLess(time.monotonic() - started, 0.7)
        source.send(call(24))
        self.assertFalse(output.get()["result"]["isError"])
        self.assertEqual(self.seen, ["synthetic-2", "synthetic-24"])

    def test_verified_tls_read_cancellation_keeps_certificate_checks(self):
        base = Path(self.tmp.name)
        cert = base / "synthetic-cert.pem"
        key = base / "synthetic-key.pem"
        config = base / "synthetic-tls.conf"
        config.write_text(
            "[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=extensions\n[dn]\nCN=localhost\n[extensions]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
        )
        subprocess.run(
            [
                "/usr/bin/openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-config",
                str(config),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=10,
        )
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
        self.http.socket = server_context.wrap_socket(
            self.http.socket, server_side=True
        )
        context = ssl.create_default_context(cafile=str(cert))
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.client.endpoint = "https://127.0.0.1:" + str(self.http.server_port)
        self.client.opener = build_opener(
            ProxyHandler({}),
            NoRedirect(),
            ReadHTTPHandler(),
            ReadHTTPSHandler(context=context),
        )
        source, output = self.start()
        source.send(call(2))
        self.assertTrue(self.started.wait(1))
        source.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 2},
            }
        )
        self.assertEqual(output.get()["error"]["code"], -32800)
        source.send(call(24))
        self.assertFalse(output.get()["result"]["isError"])
        self.assertEqual(self.seen, ["synthetic-2", "synthetic-24"])

    def test_cancel_and_deadline_during_dns_do_not_hold_queue_or_send_late_http(self):
        import socket

        original = socket.getaddrinfo
        for cancel in (True, False):
            resolving = threading.Event()
            release = threading.Event()
            finished = threading.Event()

            def lookup(*args, **kwargs):
                if not resolving.is_set():
                    resolving.set()
                    release.wait(2)
                    try:
                        return original(*args, **kwargs)
                    finally:
                        finished.set()
                return original(*args, **kwargs)

            with patch("remote_http.socket.getaddrinfo", side_effect=lookup):
                source, output = self.start(request_timeout=0.15 if not cancel else 2)
                source.send(call(1))
                self.assertTrue(resolving.wait(1))
                if cancel:
                    source.send(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/cancelled",
                            "params": {"requestId": 1},
                        }
                    )
                self.assertEqual(
                    output.get()["error"]["code"], -32800 if cancel else -32001
                )
                source.send(call(24))
                self.assertFalse(output.get()["result"]["isError"])
                self.assertNotIn("synthetic-1", self.seen)
                release.set()
                self.assertTrue(finished.wait(1))

    def test_helper_cancel_reaps_only_owned_synthetic_child(self):
        control = RequestControl(2)
        result = []
        started = threading.Event()

        def helper():
            try:
                with control.activate():
                    started.set()
                    controlled_run(
                        subprocess.run,
                        [
                            sys.executable,
                            "-I",
                            "-B",
                            "-c",
                            "import time;time.sleep(10)",
                        ],
                        input=b"",
                        capture_output=True,
                        timeout=10,
                    )
            except RequestStopped as error:
                result.append(error.reason)
            finally:
                control.close()

        worker = threading.Thread(target=helper)
        worker.start()
        self.assertTrue(started.wait(1))
        time.sleep(0.05)
        control.cancel()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ["request_cancelled"])

    def test_bounded_queue_duplicate_ids_and_malformed_input_do_not_echo(self):
        source, output = self.start(max_pending=2)
        source.send(call(1))
        self.assertTrue(self.started.wait(1))
        source.send(call(3))
        source.send(call(3))
        source.send(call(4))
        first = output.get()
        second = output.get()
        self.assertEqual(
            {first["error"]["code"], second["error"]["code"]}, {-32600, -32000}
        )
        source.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 1},
            }
        )
        self.assertEqual(output.get()["error"]["code"], -32800)
        self.assertFalse(output.get()["result"]["isError"])
        output_file = io.StringIO()
        serve(self.server, io.BytesIO(b'{"SYNTHETIC_PRIVATE_INPUT":\n'), output_file)
        self.assertNotIn("SYNTHETIC_PRIVATE_INPUT", output_file.getvalue())


if __name__ == "__main__":
    unittest.main()
