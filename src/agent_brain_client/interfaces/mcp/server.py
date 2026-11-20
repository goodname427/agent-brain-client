"""Bounded MCP request scheduling and framing, independent of content policy."""

from typing import Any
from agent_brain_client.interfaces.mcp.framing import (
    ProtocolError,
    pairs,
    keys,
    write_response,
    MAX_MESSAGE,
    ContentParameterError,
    ContentToolUnavailable,
)
from collections import deque
from dataclasses import dataclass
import hashlib
import json
from queue import Empty, Queue
import subprocess
import sys
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


# The fixed launcher owns the host stream. The worker's startup record and drain
# request are private to the child pipes and never forwarded to the host.
HANDOFF_VERSION = 1
DRAIN_METHOD = "agentbrain/private/drain"
DRAIN_PREFIX = DRAIN_METHOD + "/"
UNKNOWN_OUTCOME = {
    "code": -32003,
    "message": "MCP worker exited before acknowledging the request.",
    "data": {"outcome": "unknown", "automatic_replay": False},
}


@dataclass(frozen=True)
class WorkerSpec:
    command: tuple
    fingerprint: str
    cache_abi: str


def runtime_files(manifest):
    return {path: sha for path, sha in manifest["files"].items() if path.startswith("src/")}


def runtime_fingerprint(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def tool_contract(tools):
    result = {}
    for tool in tools:
        name = tool["name"]
        if not isinstance(name, str) or name in result:
            raise ValueError("Unique named MCP tools required")
        result[name] = {
            key: value for key, value in tool.items()
            if key not in ("description", "title", "icons")
        }
    return result


def _pump(source, events, owner, *, limit=-1, slots=None, stop=None):
    try:
        while True:
            if slots is not None:
                while not slots.acquire(timeout=0.1):
                    if stop.is_set():
                        return
                if stop.is_set():
                    return
            line = source.readline(limit)
            events.put((owner, line or None, time.monotonic()))
            if not line:
                return
            if limit >= 0 and len(line) >= limit:
                return
    except (OSError, ValueError):
        events.put((owner, None, time.monotonic()))


class _Worker:
    def __init__(self, spec, protocol, options, events, timeout):
        self.spec = spec
        self.deadline = time.monotonic() + timeout
        self.process = subprocess.Popen(
            [*spec.command, "--managed-stdio", protocol or "initial"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            **options,
        )
        self.reader = threading.Thread(
            target=_pump,
            args=(self.process.stdout, events, self),
            daemon=True,
            name="agentbrain-worker-output",
        )
        try:
            self.reader.start()
        except RuntimeError:
            self.process.kill()
            self.close_input()
            self.process.wait()
            self.process.stdout.close()
            raise
        self.contract = None

    def send(self, line):
        self.process.stdin.write(line)
        self.process.stdin.flush()

    def close_input(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass

    def finish(self, *, abort=False):
        if abort and self.process.poll() is None:
            self.process.kill()
        self.close_input()
        self.process.wait()
        self.reader.join()
        self.process.stdout.close()


def _response(rid, error):
    return {"jsonrpc": "2.0", "id": rid, "error": error}


def _line(value):
    return json.dumps(value, ensure_ascii=True).encode("ascii") + b"\n"


def _read_contract(record):
    fields = (
        "abWorkerReady", "versions", "capabilities", "toolContract", "cacheIdentity", "cacheAbi", "tools",
    )
    keys(record, fields, fields)
    if type(record["abWorkerReady"]) is not int or record.pop("abWorkerReady") != HANDOFF_VERSION:
        raise ValueError("Worker readiness record required")
    if not isinstance(record["versions"], list) or not isinstance(record["tools"], list):
        raise ValueError("Invalid MCP protocol or tool catalog")
    tool_contract(record["tools"])
    return record


def _compatible_catalog(first, second, protocol):
    return (
        protocol in second["versions"]
        and first["capabilities"] == second["capabilities"]
        and first["toolContract"] == second["toolContract"]
        and first["cacheIdentity"] == second["cacheIdentity"]
        and first["cacheAbi"] == second["cacheAbi"]
        and tool_contract(first["tools"]) == tool_contract(second["tools"])
    )


def supervise_stdio(
    initial, observe, source, output, options, *,
    poll_interval=2, probe_timeout=20, request_timeout=90, drain_timeout=90, max_pending=255,
    can_switch=None,
):
    """Keep one host session while locally verified workers change.

    One loop owns request routing. The old worker acknowledges a FIFO barrier
    through its existing executor. New requests wait behind that barrier and
    cancellation still reaches the worker that owns the request.
    """
    host, release = object(), object()
    events = Queue()
    host_slots = threading.BoundedSemaphore(max_pending)
    halt = threading.Event()
    active = candidate = None
    catalog = protocol = None
    initialized = restart_attempted = input_closed = oversized = False
    drain_id = None
    drain_deadline = None
    drain_acked = False
    drain_sequence = 0
    last_good = initial
    rejected = None
    latest = initial
    pending = {}
    waiting = deque()

    def start(spec, seed):
        return _Worker(spec, seed, options, events, probe_timeout)

    def watch():
        while not halt.wait(poll_interval):
            try:
                spec = observe()
            except Exception:
                continue  # A transaction in progress is retried on the next tick.
            if spec is not None:
                events.put((release, spec, None))

    def registration_current():
        try:
            return can_switch is None or can_switch()
        except Exception:
            return False

    def report(code):
        print(json.dumps({"stage": "mcp-handoff", "error_code": code}), file=sys.stderr)

    def close_candidate(code):
        nonlocal candidate, drain_id, rejected
        if code != "host-input-closed":
            report(code)
        rejected = candidate.spec.fingerprint
        candidate.finish(abort=True)
        candidate = None
        drain_id = None

    def worker_lost():
        nonlocal active, drain_id, restart_attempted, candidate, rejected
        lost = active
        active = None
        drain_id = None
        if lost.spec.fingerprint != last_good.fingerprint:
            rejected = lost.spec.fingerprint
        for rid, (_, owner) in list(pending.items()):
            if owner is lost:
                pending.pop(rid)
                write_response(output, _response(rid, UNKNOWN_OUTCOME))
        lost.finish(abort=True)
        report("worker-exited")
        if candidate is not None:
            candidate.finish(abort=True)
            candidate = None
        if restart_attempted:
            return
        restart_attempted = True
        try:
            candidate = start(last_good, protocol)
        except (OSError, RuntimeError):
            candidate = None
            report("worker-restart-failed")

    def dispatch(line, rid, method, deadline=None):
        if rid is not None:
            if time.monotonic() >= deadline:
                pending.pop(rid, None)
                write_response(output, _response(rid, QUEUE_DEADLINE_ERROR))
                return
            pending[rid] = (method, active)
            # The managed worker uses the same monotonic clock. Keep the host's
            # original JSON bytes and its original deadline on either worker.
            header = f"@{int(deadline * 1_000_000_000):016x} ".encode("ascii")
            line = header + line
        try:
            active.send(line)
        except (OSError, ValueError):
            worker_lost()

    def expire_waiting(now):
        nonlocal waiting
        if not waiting or waiting[0][3] > now:
            return
        remaining = deque()
        for item in waiting:
            _, rid, _, deadline = item
            if rid is not None and deadline <= now:
                pending.pop(rid)
                write_response(output, _response(rid, QUEUE_DEADLINE_ERROR))
            else:
                remaining.append(item)
        waiting = remaining

    def flush_waiting():
        while waiting and active is not None and active.contract is not None and drain_id is None:
            dispatch(*waiting.popleft())

    def receive(line, received):
        nonlocal initialized
        rid = None
        try:
            if len(line) > MAX_MESSAGE:
                raise ProtocolError(-32600, "Message exceeds 128 KB; connection closed.")
            request = json.loads(line.decode("utf-8"), object_pairs_hook=pairs)
            if not isinstance(request, dict):
                request = {}
            method = request.get("method")
            if type(request.get("id")) in (str, int):
                rid = request["id"]
            if (
                method == DRAIN_METHOD or rid in pending
                or isinstance(rid, str) and rid.startswith(DRAIN_PREFIX)
            ):
                rid = None
                raise ProtocolError(-32600, "Request ID unavailable.")
            if request.get("jsonrpc") == "2.0" and method == "notifications/cancelled" and "id" not in request:
                params = request.get("params")
                target = params.get("requestId") if isinstance(params, dict) else None
                if type(target) not in (str, int) or target not in pending:
                    return
                _, owner = pending[target]
                if owner is None:
                    for item in waiting:
                        if item[1] == target:
                            waiting.remove(item)
                            break
                    pending.pop(target)
                    write_response(output, _response(target, {
                        "code": -32800, "message": "request_cancelled",
                        "data": {"stage": "queue"},
                    }))
                elif owner is active:
                    dispatch(line, None, method)
                return
            if len(pending) >= max_pending and rid is not None:
                raise ProtocolError(-32000, "MCP request queue full; retry after active work completes.")
            if (
                request.get("jsonrpc") == "2.0"
                and (protocol is not None or any(value[0] == "initialize" for value in pending.values()))
                and method == "notifications/initialized" and "id" not in request
                and not set(request) - {"jsonrpc", "method", "params"}
                and (not isinstance(request.get("params"), dict)
                     or isinstance(request["params"].get("_meta", {}), dict))
            ):
                initialized = True
            deadline = received + request_timeout
            if rid is not None and deadline <= time.monotonic():
                write_response(output, _response(rid, QUEUE_DEADLINE_ERROR))
                return
            if active is None or active.contract is None or drain_id is not None:
                if len(waiting) >= max_pending:
                    if rid is None:
                        return
                    raise ProtocolError(-32000, "MCP request queue full; retry after active work completes.")
                waiting.append((line, rid, method, deadline))
                if rid is not None:
                    pending[rid] = (method, None)
            else:
                dispatch(line, rid, method, deadline)
        except ProtocolError as error:
            write_response(output, _response(rid, {"code": error.code, "message": error.message}))
        except (ValueError, UnicodeError):
            write_response(output, _response(rid, {
                "code": -32700, "message": "Invalid JSON message.",
            }))

    try:
        active = start(initial, None)
        threading.Thread(
            target=_pump,
            args=(source, events, host),
            kwargs={"limit": MAX_MESSAGE + 1, "slots": host_slots, "stop": halt},
            daemon=True,
            name="agentbrain-host-input",
        ).start()
        threading.Thread(target=watch, daemon=True, name="agentbrain-release-observer").start()
        while True:
            if input_closed and not pending and not waiting:
                return 2 if oversized else 0
            if active is None and candidate is None:
                for rid in pending:
                    write_response(output, _response(rid, {
                        "code": -32003, "message": "MCP worker unavailable before dispatch.",
                        "data": {"outcome": "not-dispatched"},
                    }))
                return 2
            now = time.monotonic()
            expire_waiting(now)
            if active is not None and active.contract is None and now >= active.deadline:
                worker_lost()
            if candidate is not None and candidate.contract is None and now >= candidate.deadline:
                close_candidate("worker-readiness-timeout")
                flush_waiting()
            if drain_id is not None and drain_acked:
                if candidate.process.poll() is not None:
                    close_candidate("candidate-exited")
                    flush_waiting()
                elif not any(owner is active for _, owner in pending.values()):
                    if registration_current():
                        previous, active, candidate = active, candidate, None
                        last_good = previous.spec
                        restart_attempted = False
                        drain_id = None
                        previous.finish()
                    else:
                        close_candidate("native-registration-changed")
                    flush_waiting()
            if drain_id is not None and now >= drain_deadline:
                close_candidate("worker-drain-timeout")
                flush_waiting()
            if (
                not input_closed and active is not None and active.contract is not None
                and initialized and protocol is not None and candidate is None
                and latest and latest.fingerprint not in (active.spec.fingerprint, rejected)
            ):
                if active.spec.cache_abi != latest.cache_abi:
                    rejected = latest.fingerprint
                    report("cache-contract-changed")
                else:
                    try:
                        candidate = start(latest, protocol)
                    except (OSError, RuntimeError):
                        rejected = latest.fingerprint
                        report("worker-start-failed")
            try:
                owner, line, received = events.get(timeout=0.1)
            except Empty:
                continue
            if owner is host:
                host_slots.release()
                if line is not None:
                    oversized = len(line) > MAX_MESSAGE
                    if not oversized and not line.endswith(b"\n") and len(line) < MAX_MESSAGE:
                        line += b"\n"
                    receive(line, received)
                if line is None or oversized:
                    input_closed = True
                    if active is not None and candidate is not None:
                        close_candidate("host-input-closed")
                        flush_waiting()
                    if active is not None and not waiting:
                        active.close_input()
            elif owner is release:
                latest = line
                if active is not None and candidate is not None and candidate.spec != latest:
                    close_candidate("candidate-superseded")
                    flush_waiting()
            elif owner is active or owner is candidate:
                if line is None:
                    if owner is candidate:
                        close_candidate("candidate-exited")
                        flush_waiting()
                    else:
                        worker_lost()
                    continue
                if owner.contract is None:
                    try:
                        owner.contract = _read_contract(json.loads(line, object_pairs_hook=pairs))
                        if owner.contract["cacheAbi"] != owner.spec.cache_abi:
                            raise ValueError("MCP worker cache ABI changed")
                        if catalog is not None and not _compatible_catalog(
                            catalog, owner.contract, protocol
                        ):
                            raise ValueError("MCP catalog changed")
                    except (AttributeError, KeyError, ProtocolError, TypeError, ValueError):
                        if owner is candidate:
                            close_candidate("worker-readiness-rejected")
                            flush_waiting()
                        else:
                            worker_lost()
                        continue
                    if owner is active or active is None:
                        active, candidate = owner, None
                        catalog = owner.contract if catalog is None else catalog
                        flush_waiting()
                        if input_closed and not waiting:
                            active.close_input()
                    else:
                        drain_sequence += 1
                        drain_id = DRAIN_PREFIX + str(drain_sequence)
                        drain_deadline = time.monotonic() + drain_timeout
                        drain_acked = False
                        try:
                            active.send(_line({
                                "jsonrpc": "2.0", "id": drain_id, "method": DRAIN_METHOD,
                            }))
                        except (OSError, ValueError):
                            worker_lost()
                    continue
                # A ready candidate is idle until the barrier on the old worker.
                if owner is candidate:
                    close_candidate("unexpected-candidate-output")
                    flush_waiting()
                    continue
                try:
                    response = json.loads(line)
                    rid = response["id"]
                    if rid is not None and type(rid) not in (str, int):
                        raise ValueError("Unexpected worker response ID")
                except (KeyError, TypeError, ValueError):
                    worker_lost()
                    continue
                if isinstance(rid, str) and rid.startswith(DRAIN_PREFIX):
                    if rid != drain_id:
                        continue
                    if response.get("result") == {}:
                        drain_acked = True
                    else:
                        close_candidate("worker-drain-failed")
                        flush_waiting()
                    continue
                method, _ = pending.pop(rid, (None, None))
                result = response.get("result")
                if method == "initialize" and isinstance(result, dict):
                    negotiated = result.get("protocolVersion")
                    if negotiated in catalog["versions"]:
                        protocol = negotiated
                if method == "tools/list" and isinstance(result, dict) and "tools" in result:
                    response["result"] = dict(result, tools=catalog["tools"])
                write_response(output, response)
    finally:
        halt.set()
        if candidate is not None:
            candidate.finish(abort=True)
        if active is not None:
            active.finish()
