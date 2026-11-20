"""Request-local cancellation and deadlines, independent of transports."""

from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time


class RequestStopped(Exception):
    """Safe cancellation/deadline signal; never includes transport or secrets."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


_request_scope = threading.local()


class RequestControl:
    """One MCP request, including queue time; abort only its owned read I/O."""

    def __init__(self, timeout):
        started = time.monotonic()
        self.deadline = started + timeout
        timing = _recall_timing.get()
        if timing is not None:
            timing.phases["control_started"] = round((started - timing.started) * 1000, 3)
        self.lock = threading.Lock()
        self.reason = None
        self.finished = False
        self.aborters = []
        self.timer = threading.Timer(
            timeout, self.cancel, args=("request_deadline_exceeded",)
        )
        self.timer.daemon = True
        self.timer.start()

    def cancel(self, reason="request_cancelled"):
        with self.lock:
            if self.finished or self.reason is not None:
                return
            self.reason = reason
            aborters = list(self.aborters)
        for abort in aborters:
            try:
                abort()
            except Exception:
                pass

    def check(self):
        if time.monotonic() >= self.deadline:
            self.cancel("request_deadline_exceeded")
        with self.lock:
            reason = self.reason
        if reason is not None:
            raise RequestStopped(reason)

    def register(self, abort):
        with self.lock:
            stopped = self.reason is not None
            if not stopped and not self.finished:
                self.aborters.append(abort)
        if stopped:
            try:
                abort()
            except Exception:
                pass
            self.check()

    def close(self, *, check=False):
        self.timer.cancel()
        with self.lock:
            if check:
                reason = self.reason
                if reason is None and time.monotonic() >= self.deadline:
                    reason = "request_deadline_exceeded"
                if reason is not None:
                    raise RequestStopped(reason)
            self.finished = True
            self.aborters.clear()

    @contextmanager
    def activate(self):
        previous = getattr(_request_scope, "control", None)
        _request_scope.control = self
        try:
            self.check()
            yield
            self.check()
        finally:
            _request_scope.control = previous


def current_request():
    return getattr(_request_scope, "control", None)


def check_request():
    control = current_request()
    if control is not None:
        control.check()


def request_timeout(timeout):
    control = current_request()
    if control is None:
        return timeout
    control.check()
    return max(0.001, min(timeout, control.deadline - time.monotonic()))


RECALL_TIMING_ENV = "AGENT_BRAIN_RECALL_TIMING"
_HEADER_READ_SAMPLES = 8
_recall_timing = ContextVar("recall_timing", default=None)


class _RecallTiming:
    def __init__(self):
        self.started = time.monotonic()
        self.request_key = None
        self.phases = {}
        self.in_headers = False
        self.read_count = 0
        self.first_read_budget = None
        self.reads = []
        self.last_read = None
        self.failed_at = None

    def offset(self):
        return round((time.monotonic() - self.started) * 1000, 3)


@contextmanager
def recall_timing_scope(enabled):
    token = _recall_timing.set(_RecallTiming() if enabled else None)
    try:
        yield
    finally:
        _recall_timing.reset(token)


def start_recall_timing(request_key):
    timing = _recall_timing.get()
    if timing is not None:
        timing.request_key = request_key
        timing.phases["receipt_started"] = timing.offset()


def recall_timing_active():
    timing = _recall_timing.get()
    return timing is not None and timing.request_key is not None


@contextmanager
def recall_timing_stage(phase):
    timing = _recall_timing.get()
    if timing is None or timing.request_key is None:
        yield
        return
    started = timing.offset()
    try:
        yield
    finally:
        timing.phases[phase] = [started, timing.offset()]


@contextmanager
def recall_response_headers():
    timing = _recall_timing.get()
    if timing is None:
        yield
        return
    timing.in_headers = True
    try:
        yield
    finally:
        timing.in_headers = False


@contextmanager
def recall_header_read(socket_timeout):
    timing = _recall_timing.get()
    if timing is None or not timing.in_headers:
        yield
        return
    started = timing.offset()
    if timing.read_count == 0:
        control = current_request()
        remaining = (
            round(max(0, control.deadline - time.monotonic()) * 1000, 3)
            if control is not None else None
        )
        timing.first_read_budget = {
            "request_ms": remaining,
            "socket_ms": round(socket_timeout * 1000, 3) if socket_timeout is not None else None,
        }
    timing.read_count += 1
    try:
        yield
    finally:
        timing.last_read = [started, timing.offset()]
        if len(timing.reads) < _HEADER_READ_SAMPLES:
            timing.reads.append(timing.last_read)


def recall_timing_failed():
    timing = _recall_timing.get()
    if timing is not None:
        timing.failed_at = timing.offset()


def saved_recall_timing(request_key):
    timing = _recall_timing.get()
    if timing is None or timing.request_key != request_key or timing.failed_at is None:
        return None
    finished = timing.offset()
    return {
        "request_key": request_key,
        "phase_offsets_ms": dict(timing.phases, failed=timing.failed_at, receipt_saved=finished),
        "first_read_budget_ms": timing.first_read_budget,
        "response_header_reads": {
            "count": timing.read_count,
            "first_spans_ms": timing.reads,
            "last_span_ms": timing.last_read,
        },
        "failure_receipt_write_ms": round(finished - timing.failed_at, 3),
    }
