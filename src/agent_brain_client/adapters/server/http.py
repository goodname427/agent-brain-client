"""Thin JSON-line HTTP adapter. No Core, repository, Git or host discovery."""

from pathlib import Path

from agent_brain_client.application.control import (
    check_request,
    current_request,
    request_timeout,
)
#!/usr/bin/env python3


import http.client
import io
import json
import math
import re
import select
import threading
import time
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPHandler,
    HTTPSHandler,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)


_resolver_slots = threading.BoundedSemaphore(8)


def _watch_socket(sock):
    control = current_request()
    if control is None:
        return

    def abort():
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    control.register(abort)
    control.check()


def _read_socket(address, timeout=None, source_address=None):
    """Register the owned socket before connect, not only after HTTP headers."""
    check_request()
    candidates = _resolve_address(address)
    check_request()
    error = None
    for family, kind, protocol, _, destination in candidates:
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            _watch_socket(sock)
            sock.connect(destination)
            check_request()
            return sock
        except OSError as exc:
            sock.close()
            check_request()
            error = exc
        except BaseException:
            sock.close()
            raise
    if error is not None:
        raise error
    raise OSError("No connection address")


def _resolve_address(address):
    control = current_request()
    if control is None:
        return socket.getaddrinfo(address[0], address[1], type=socket.SOCK_STREAM)
    control.check()
    if not _resolver_slots.acquire(blocking=False):
        raise OSError("Name resolver busy")
    done = threading.Event()
    result = []
    errors = []

    def resolve():
        try:
            result.append(
                socket.getaddrinfo(address[0], address[1], type=socket.SOCK_STREAM)
            )
        except Exception as error:
            errors.append(error)
        finally:
            _resolver_slots.release()
            done.set()

    # OS DNS is not interruptible. Only a bounded, credential-free lookup may
    # finish late; it cannot send HTTP, touch the adapter or publish its result.
    threading.Thread(target=resolve, name="content-read-dns", daemon=True).start()
    control.register(done.set)
    if not done.wait(request_timeout(90)):
        control.cancel("request_deadline_exceeded")
    control.check()
    if errors:
        raise errors[0]
    return result[0]


def _wait_read_socket(sock, control, deadline, *, write=False):
    # Winsock can keep a blocking recv/select asleep after another thread calls
    # shutdown/close. Poll readiness without entering a long native receive.
    while True:
        control.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Read transport deadline exceeded")
        try:
            readable, writable, _ = select.select(
                [] if write else [sock],
                [sock] if write else [],
                [],
                min(0.05, remaining),
            )
        except (OSError, ValueError):
            control.check()
            raise
        control.check()
        if readable or writable:
            return


class _ReadSocketFile(io.RawIOBase):
    """Nonblocking HTTP/TLS receive with an owned socket makefile lifetime."""

    def __init__(self, sock, control):
        super().__init__()
        self.sock, self.control = sock, control
        self.timeout = sock.gettimeout()
        # Keep the descriptor alive when urllib closes the connection object
        # after headers. Releasing this lease closes it just like socket.makefile.
        self.lease = sock.makefile("rb", buffering=0)
        sock.setblocking(False)

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.closed:
            raise ValueError("Read file is closed")
        deadline = self.control.deadline
        if self.timeout is not None:
            deadline = min(deadline, time.monotonic() + self.timeout)
        while True:
            self.control.check()
            try:
                return self.sock.recv_into(buffer)
            except ssl.SSLWantWriteError:
                _wait_read_socket(self.sock, self.control, deadline, write=True)
            except (BlockingIOError, ssl.SSLWantReadError):
                _wait_read_socket(self.sock, self.control, deadline)
            except OSError:
                self.control.check()
                raise

    def close(self):
        try:
            lease = getattr(self, "lease", None)
            if not self.closed and lease is not None:
                lease.close()
        finally:
            super().close()


class _ReadResponseSocket:
    def __init__(self, sock, control):
        self.sock, self.control = sock, control

    def makefile(self, mode):
        if mode != "rb":
            raise ValueError("Read-only response required")
        return io.BufferedReader(_ReadSocketFile(self.sock, self.control))


class ReadHTTPResponse(http.client.HTTPResponse):
    def __init__(self, sock, *args, **kwargs):
        control = current_request()
        if control is not None:
            sock = _ReadResponseSocket(sock, control)
        super().__init__(sock, *args, **kwargs)


class ReadHTTPConnection(http.client.HTTPConnection):
    response_class = ReadHTTPResponse

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _read_socket

    def connect(self):
        check_request()
        super().connect()


class ReadHTTPSConnection(http.client.HTTPSConnection):
    response_class = ReadHTTPResponse

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _read_socket

    def connect(self):
        check_request()
        http.client.HTTPConnection.connect(self)
        server_hostname = getattr(self, "_tunnel_host", None) or self.host
        self.sock = getattr(self, "_context").wrap_socket(
            self.sock, server_hostname=server_hostname, do_handshake_on_connect=False
        )
        _watch_socket(self.sock)
        self.sock.do_handshake()
        check_request()


class ReadHTTPHandler(HTTPHandler):
    def http_open(self, req):
        return self.do_open(ReadHTTPConnection, req)


class ReadHTTPSHandler(HTTPSHandler):
    def __init__(self, debuglevel=0, context=None):
        super().__init__(debuglevel, context=context)
        self.read_context = context

    def https_open(self, req):
        return self.do_open(ReadHTTPSConnection, req, context=self.read_context)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RemoteClient:
    def __init__(
        self,
        endpoint,
        token_file=None,
        data_mode="synthetic",
        *,
        credential_provider=None,
        write_receipt=None,
        adapter="codex",
    ):
        if data_mode not in ("synthetic", "personal"):
            raise ValueError("Unsupported data mode")
        if write_receipt not in (None, "local-durable-v1"):
            raise ValueError("Unsupported write receipt")
        if not isinstance(adapter, str) or not re.fullmatch(
            r"[a-z][a-z0-9-]{0,63}", adapter
        ):
            raise ValueError("Invalid adapter identity")
        self.adapter = adapter
        self.write_receipt = write_receipt
        self.data_mode = data_mode
        url = urlsplit(endpoint)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
            or url.scheme == "http"
            and url.hostname not in ("127.0.0.1", "localhost", "::1")
        ):
            raise ValueError(
                "Use HTTPS, or loopback HTTP through an approved SSH tunnel."
            )
        if (token_file is None) == (credential_provider is None):
            raise ValueError("One internal credential source required")
        if credential_provider is not None and url.scheme != "https":
            raise ValueError("Device credentials require HTTPS")
        self.endpoint, self.token_file = (
            endpoint.rstrip("/"),
            Path(token_file) if token_file is not None else None,
        )
        self.credential_provider = credential_provider
        self.opener = build_opener(
            ProxyHandler({}), NoRedirect(), ReadHTTPHandler(), ReadHTTPSHandler()
        )

    def request(self, request, *, timeout=90, deadline=None):
        if deadline is None:
            return self._request(request, timeout=timeout)
        if not isinstance(request, dict) or request.get("method") != "client_updates":
            raise ValueError("A bounded probe may only discover the read-only policy.")
        # One short-lived daemon worker in this MCP process, never a service/job.
        # A slow HTTP read must not delay initialize; late results cannot change
        # instructions, and no late writes are possible on this read-only path.
        done = threading.Event()
        result = []

        def probe():
            try:
                result.append(self._request(request, timeout=timeout))
            except Exception:
                result.append(None)
            finally:
                done.set()

        threading.Thread(target=probe, daemon=True).start()
        if done.wait(deadline) and result and result[0] is not None:
            return result[0]
        return {
            "request_id": request.get("request_id"),
            "error": {"code": "transport_unconfirmed", "retryable": True},
            "persistence": {"durable": False},
        }

    def _request(self, request, *, timeout=90):
        check_request()
        rid = request.get("request_id") if isinstance(request, dict) else None
        if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", rid):
            raise ValueError(
                "Supply a stable request_id; retry a write with the same ID and parameters."
            )
        token = self._token()
        check_request()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
            raise ValueError("Invalid credential source")
        data = json.dumps(request, ensure_ascii=False).encode()
        if len(data) > 131072:
            raise ValueError("Request exceeds 128 KB")
        req = Request(
            self.endpoint + "/v1/requests",
            data=data,
            method="POST",
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        req.add_header("X-Agent-Brain-Adapter", self.adapter)
        req.add_header("X-Agent-Brain-Protocol", "1")
        req.add_header("X-Agent-Brain-State-Schema", "1")
        if self.data_mode == "personal":
            req.add_header("X-Agent-Brain-Data-Mode", "personal")
        if self.write_receipt is not None and request.get("method") in (
            "remember",
            "forget",
            "content_put",
            "content_remove",
            "content_set_enabled",
        ):
            req.add_header("X-Agent-Brain-Write-Receipt", self.write_receipt)
        write = request.get("method") in (
            "remember",
            "forget",
            "content_put",
            "content_remove",
            "content_set_enabled",
            "memory_event_submit",
            "memory_extract",
        )
        core = (
            request.get("method")
            in ("memory_event_submit", "memory_extract", "memory_global_query")
            or request.get("method") == "memory_query"
            and isinstance(request.get("params"), dict)
            and ("context" in request["params"] or "origin" in request["params"])
        )

        def response_json(raw):
            if not core:
                return json.loads(raw)

            def unique(items):
                value = {}
                for key, item in items:
                    if key in value:
                        raise ValueError("Duplicate Core response field")
                    value[key] = item
                return value

            def reject_constant(value):
                raise ValueError("Nonfinite Core response number")

            def finite_float(value):
                number = float(value)
                if not math.isfinite(number):
                    raise ValueError("Nonfinite Core response number")
                return number

            return json.loads(
                raw,
                object_pairs_hook=unique,
                parse_constant=reject_constant,
                parse_float=finite_float,
            )

        def failed(code, stage, retryable=False, **metadata):
            return {
                "request_id": rid,
                "error": {
                    "code": code,
                    "retryable": retryable,
                    "stage": stage,
                    **metadata,
                },
                "persistence": {"durable": False},
            }

        try:
            with self.opener.open(req, timeout=request_timeout(timeout)) as response:
                value = response.read(262145)
                status = getattr(response, "status", None)
            check_request()
            if len(value) > 262144:
                return failed("response_too_large", "response-parse", write)
            try:
                result = response_json(value)
            except (ValueError, UnicodeError):
                return failed("invalid_response_json", "response-parse", write)
            if not isinstance(result, dict):
                return failed("invalid_response_shape", "response-validation", write)
            if result.get("request_id") != rid:
                return failed("response_id_mismatch", "response-validation", write)
            if "error" in result:
                error = result["error"]
                if (
                    not isinstance(error, dict)
                    or not isinstance(error.get("code"), str)
                    or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error["code"])
                    or type(error.get("retryable")) is not bool
                ):
                    return failed(
                        "invalid_response_error", "response-validation", write
                    )
                metadata = {"http_status": status} if type(status) is int else {}
                return failed(error["code"], "http", error["retryable"], **metadata)
            if write and "error" not in result:
                if request.get("method") == "memory_extract":
                    p = result.get("persistence", {})
                    r = result.get("result", {})
                    valid = (
                        isinstance(p, dict)
                        and p.get("state") == "core-extracted"
                        and p.get("durable") is False
                        and p.get("receipt_ledger_durable") is True
                        and p.get("preimage_retained") is True
                        and isinstance(r, dict)
                        and r.get("state") == "succeeded"
                        and r.get("stages") == ["L1", "L2", "L3"]
                        and type(r.get("http_requests")) is int
                        and r["http_requests"] == 3
                        and isinstance(r.get("job_id"), str)
                        and re.fullmatch(r"abj-[a-f0-9]{64}", r["job_id"])
                    )
                    return (
                        result
                        if valid
                        else failed(
                            "invalid_extraction_receipt", "response-validation", False
                        )
                    )
                if request.get("method") == "memory_event_submit":
                    from agent_brain_client.domain.contract import core_receipt

                    return (
                        result
                        if core_receipt(result, request)
                        else failed(
                            "invalid_write_receipt", "response-validation", True
                        )
                    )
                receipt = result.get("persistence", {})
                native = (
                    isinstance(receipt, dict)
                    and request.get("method") == "content_put"
                    and request.get("params", {}).get("kind") == "memory"
                    and receipt.get("storage") == "tencent-l1"
                    and isinstance(receipt.get("checkpoint"), str)
                    and re.fullmatch(r"[a-f0-9]{64}", receipt["checkpoint"])
                    and "commit" not in receipt
                )
                local = (
                    self.write_receipt == "local-durable-v1"
                    and isinstance(receipt, dict)
                    and receipt.get("state") == "local-confirmed"
                    and receipt.get("durable") is False
                    and receipt.get("local_durable") is True
                    and receipt.get("github_confirmed") is False
                    and (
                        native
                        or isinstance(receipt.get("commit"), str)
                        and re.fullmatch(r"[a-f0-9]{40}", receipt["commit"])
                    )
                    and isinstance(result.get("result"), dict)
                )
                if (
                    not isinstance(receipt, dict)
                    or receipt.get("durable") is not True
                    and not local
                ):
                    return failed("invalid_write_receipt", "response-validation", True)
            return result
        except HTTPError as error:
            # Redirects are never followed with the bearer credential.
            status = error.code
            try:
                raw = error.read(262145 if core else 262144)
                if core and len(raw) > 262144:
                    return failed(
                        "response_too_large",
                        "response-parse",
                        write,
                        http_status=status,
                    )
                try:
                    result = response_json(raw)
                except (ValueError, UnicodeError):
                    if core:
                        return failed(
                            "invalid_response_json",
                            "response-parse",
                            write,
                            http_status=status,
                        )
                    raise
                if (
                    isinstance(result, dict)
                    and result.get("request_id") in (None, rid)
                    and isinstance(result.get("error"), dict)
                ):
                    error = result["error"]
                    code = error.get("code")
                    if (
                        isinstance(code, str)
                        and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)
                        and type(error.get("retryable")) is bool
                    ):
                        return failed(
                            code, "http", error["retryable"], http_status=status
                        )
            except (ValueError, OSError, http.client.HTTPException):
                pass
            check_request()
            return failed(
                "http_response_unconfirmed",
                "http",
                status >= 500 or status in (408, 429),
                http_status=status,
            )
        except (URLError, OSError, http.client.HTTPException) as error:
            check_request()
            reason = getattr(error, "reason", error)
            kind = (
                "timeout"
                if isinstance(reason, TimeoutError)
                else "tls"
                if isinstance(reason, ssl.SSLError)
                else "http-framing"
                if isinstance(reason, http.client.HTTPException)
                else "connection"
            )
            return failed("transport_unconfirmed", "transport", True, failure_kind=kind)

    def _token(self):
        if self.credential_provider is not None:
            return self.credential_provider()
        assert (
            self.token_file is not None
        )  # Constructor requires one credential source.
        if self.token_file.stat().st_mode & 0o077:
            raise ValueError("Token file must have mode 0600.")
        return self.token_file.read_text(encoding="utf-8").strip()
