"""Shared native Hook response dialect used by the supported Harnesses."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import sys
import threading
import time

from agent_brain_client.application.memory.context import injectable_context


CODEBUDDY_HOOK_REQUEST_SECONDS = 29.4
_native_output = ContextVar("native_hook_output", default=None)


def emit(output):
    binding = _native_output.get()
    if binding is None:
        print(json.dumps(output, ensure_ascii=False), flush=True)
        return True
    return binding[0](output)


def remaining_request_seconds(seconds):
    binding = _native_output.get()
    if binding is not None:
        # Give the timed request a chance to finalize before the hard exit.
        seconds = min(seconds, binding[1] - time.monotonic() - 0.04)
    return max(0, seconds)


@contextmanager
def native_output_deadline(seconds):
    """In the dedicated Hook child, bound non-cooperative I/O and exit once."""
    started = time.monotonic()
    deadline = started + seconds
    lock = threading.Lock()
    written = False

    def write(output):
        nonlocal written
        with lock:
            if written or time.monotonic() >= deadline:
                return False
            print(json.dumps(output, ensure_ascii=False), flush=True)
            written = True
            return time.monotonic() < deadline

    def report_timeout(elapsed_ms):
        output_unconfirmed = True
        if lock.acquire(blocking=False):
            try:
                output_unconfirmed = not written
                if output_unconfirmed:
                    print("{}", flush=True)
            finally:
                lock.release()
        print(json.dumps({"operation": "native-hook-deadline",
                          "elapsed_ms": elapsed_ms, "output_unconfirmed": output_unconfirmed}),
              file=sys.stderr, flush=True)

    def expire():
        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        try:
            # A host can stop reading either pipe. Reporting gets a fixed
            # opportunity to finish, independently of the hard process exit.
            reporter = threading.Thread(target=report_timeout, args=(elapsed_ms,), daemon=True)
            reporter.start()
            reporter.join(timeout=0.03)
        finally:
            # Durable writes already use the Client's ordinary crash recovery.
            os._exit(0)

    timer = threading.Timer(seconds, expire)
    timer.daemon = True
    token = _native_output.set((write, deadline))
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        _native_output.reset(token)


def hook_context(kind, items):
    context = injectable_context(items)
    if not context:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": kind,
            "additionalContext": context,
        }
    }
