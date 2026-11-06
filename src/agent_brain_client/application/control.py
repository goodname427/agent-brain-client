"""Request-local cancellation and deadlines, independent of transports."""

from contextlib import contextmanager
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
        self.deadline = time.monotonic() + timeout
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
