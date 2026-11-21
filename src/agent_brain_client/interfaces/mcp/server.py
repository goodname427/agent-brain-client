"""Bounded MCP request scheduling and framing, independent of content policy."""

from typing import Any
from agent_brain_client.interfaces.mcp.framing import (
    ProtocolError,
    pairs,
    write_response,
    MAX_MESSAGE,
    ContentParameterError,
    ContentToolUnavailable,
)
from collections import deque
import json
import threading
import time
from agent_brain_client.application.control import RequestControl, RequestStopped


REQUEST_DEADLINE_HEADER = 18
QUEUE_DEADLINE_ERROR = {
    "code": -32001,
    "message": "request_deadline_exceeded",
    "data": {"stage": "queue"},
}


def serve(server, source, output, *, request_timeout=90, max_pending=256, managed_stdio=False):
    """Keep one adapter executor, but receive cancellation while it is working.

    Read deadlines include queue time. No detached adapter work is left after a
    timeout: its HTTP socket/helper is aborted and its late result is discarded.
    Active writes finish their durable transaction; retries keep the original ID.
    """
    work: deque = deque()
    pending: dict[str | int, RequestControl] = {}
    state_lock = threading.Lock()
    available = threading.Condition(state_lock)
    output_lock = threading.Lock()
    disconnected = threading.Event()
    receiving = True

    def emit(rid, *, result=None, error=None):
        response = {"jsonrpc": "2.0", "id": rid}
        response["error" if error is not None else "result"] = (
            error if error is not None else result
        )
        with output_lock:
            if disconnected.is_set():
                return
            try:
                write_response(output, response)
                return
            except (OSError, ValueError):
                disconnected.set()
        # A closed output cannot acknowledge more work. Keep active durable
        # writes recoverable, but cancel reads and queued operations.
        with state_lock:
            controls = list(pending.values())
        for control in controls:
            control.cancel()

    def stopped(rid, error, stage="execution"):
        code = -32800 if error.reason == "request_cancelled" else -32001
        emit(
            rid, error={"code": code, "message": error.reason, "data": {"stage": stage}}
        )

    def stop_queued(item):
        # Cancellation must acknowledge and remove queued work even while a
        # durable write owns the executor. Never detach active adapter work.
        _, rid, control = item
        with available:
            if item not in work:
                return
            work.remove(item)
            if pending.get(rid) is control:
                pending.pop(rid)
        control.close()
        stopped(rid, RequestStopped(control.reason), "queue")

    def execute():
        while True:
            with available:
                while not work and receiving:
                    available.wait()
                if not work:
                    return
                item = work.popleft()
            request, rid, control = item
            try:
                if disconnected.is_set():
                    if control is not None:
                        control.cancel()
                    continue
                if control is not None:
                    control.check()
                params = request.get("params", {}) if isinstance(request, dict) else {}
                read_only = (
                    request.get("method") == "tools/call"
                    and isinstance(params, dict)
                    and params.get("name")
                    in (
                        "content_catalog",
                        "content_read",
                        "memory_query",
                        "memory_recall",
                    )
                )
                if control is not None and read_only:
                    with control.activate():
                        value = server.handle(request)
                    control.close(check=True)
                else:
                    # Never cancel midway through durable enqueue/CAS/rollback.
                    if control is not None:
                        control.close(check=True)
                    value = server.handle(request)
                if value is not None:
                    emit(rid, result=value)
            except RequestStopped as error:
                stopped(rid, error)
            except ProtocolError as error:
                payload: dict[str, Any] = {"code": error.code, "message": error.message}
                if isinstance(error, ContentToolUnavailable):
                    payload["data"] = {
                        "stage": "tools-ready",
                        "error_code": "tool-not-exposed",
                        "client_runtime": server.client_runtime,
                    }
                elif isinstance(error, ContentParameterError):
                    payload["data"] = {
                        "stage": "parameters",
                        "client_runtime": server.client_runtime,
                    }
                elif error.code == -32002:
                    payload["data"] = {
                        "stage": "handshake",
                        "error_code": "mcp-not-ready",
                    }
                emit(rid, error=payload)
            except Exception:
                emit(
                    rid,
                    error={
                        "code": -32603,
                        "message": "MCP operation failed; request JSON was valid.",
                    },
                )
            finally:
                if control is not None:
                    control.close()
                    with state_lock:
                        if rid is not None and pending.get(rid) is control:
                            pending.pop(rid)

    worker = threading.Thread(target=execute, name="content-mcp-executor")
    worker.start()
    line_limit = MAX_MESSAGE + 1 + (REQUEST_DEADLINE_HEADER if managed_stdio else 0)
    try:
        while True:
            raw = source.readline(line_limit)
            if not raw:
                break
            if disconnected.is_set():
                break
            rid = None
            control = None
            deadline = None
            try:
                if managed_stdio and raw.startswith(b"@"):
                    header, raw = raw[:REQUEST_DEADLINE_HEADER], raw[REQUEST_DEADLINE_HEADER:]
                    if len(header) != REQUEST_DEADLINE_HEADER or not header.endswith(b" "):
                        raise ValueError("Malformed managed MCP deadline")
                    deadline = int(header[1:-1], 16) / 1_000_000_000
                if len(raw) > MAX_MESSAGE:
                    raise ProtocolError(
                        -32600, "Message exceeds 128 KB; connection closed."
                    )
                request = json.loads(raw.decode(), object_pairs_hook=pairs)
                if isinstance(request, dict) and type(request.get("id")) in (str, int):
                    rid = request["id"]
                if (
                    isinstance(request, dict)
                    and request.get("jsonrpc") == "2.0"
                    and request.get("method") == "notifications/cancelled"
                    and "id" not in request
                ):
                    params = request.get("params", {})
                    if isinstance(params, dict) and type(params.get("requestId")) in (
                        str,
                        int,
                    ):
                        with state_lock:
                            target = pending.get(params["requestId"])
                        if target is not None:
                            target.cancel()
                    continue
                with available:
                    if disconnected.is_set():
                        break
                    if rid is not None:
                        if rid in pending:
                            raise ProtocolError(-32600, "Request ID already pending.")
                        if len(pending) >= max_pending:
                            raise ProtocolError(
                                -32000,
                                "MCP request queue full; retry after active work completes.",
                            )
                        timeout = request_timeout
                        if deadline is not None:
                            timeout = min(timeout, deadline - time.monotonic())
                            if timeout <= 0:
                                raise RequestStopped("request_deadline_exceeded")
                        control = RequestControl(timeout)
                        pending[rid] = control
                    if len(work) >= max_pending:
                        raise ProtocolError(
                            -32000,
                            "MCP request queue full; retry after active work completes.",
                        )
                    item = (request, rid, control)
                    work.append(item)
                    available.notify()
                if control is not None:
                    try:
                        control.register(lambda item=item: stop_queued(item))
                    except RequestStopped:
                        pass  # The queued callback or executor replied.
            except RequestStopped as error:
                stopped(rid, error, "queue")
            except (ProtocolError, ValueError, UnicodeError) as error:
                if control is not None:
                    control.close()
                    with state_lock:
                        if rid is not None:
                            pending.pop(rid, None)
                if isinstance(error, ProtocolError):
                    emit(rid, error={"code": error.code, "message": error.message})
                else:
                    emit(
                        rid, error={"code": -32700, "message": "Invalid JSON message."}
                    )
            if len(raw) > MAX_MESSAGE:
                break
    finally:
        with available:
            receiving = False
            available.notify()
        worker.join()


# Keep the existing worker-side import names during migration. The supervisor
# never imports this per-version business worker module.
from agent_brain_client.interfaces.mcp.launcher import (
    DRAIN_METHOD as DRAIN_METHOD,
    DRAIN_PREFIX as DRAIN_PREFIX,
    HANDOFF_VERSION as HANDOFF_VERSION,
    WorkerSpec as WorkerSpec,
    runtime_files as runtime_files,
    runtime_fingerprint as runtime_fingerprint,
    supervise_stdio as supervise_stdio,
    tool_contract as tool_contract,
)
