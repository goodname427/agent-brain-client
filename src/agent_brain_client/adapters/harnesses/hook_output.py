"""Shared native Hook response dialect used by the supported Harnesses."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import sys
import threading
import time

from agent_brain_client.application.memory.context import injectable_context


CODEBUDDY_HOOK_REQUEST_SECONDS = 75
_native_emit = ContextVar("native_hook_emit", default=None)


def emit(output):
    write = _native_emit.get()
    if write is None:
        print(json.dumps(output, ensure_ascii=False), flush=True)
    else:
        write(output)


@contextmanager
def native_output_deadline(seconds):
    """In the dedicated Hook child, bound non-cooperative I/O and exit once."""
    started = time.monotonic()
    lock = threading.Lock()
    written = False

    def write(output):
        nonlocal written
        with lock:
            if not written:
                print(json.dumps(output, ensure_ascii=False), flush=True)
                written = True

    def expire():
        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        with lock:
            try:
                if not written:
                    print("{}", flush=True)
                print(json.dumps({"operation": "native-hook-deadline",
                                  "elapsed_ms": elapsed_ms, "output_unconfirmed": not written}),
                      file=sys.stderr, flush=True)
            finally:
                # The dedicated child can exit even if native I/O never returns.
                # Durable writes already use the Client's ordinary crash recovery.
                os._exit(0)

    timer = threading.Timer(seconds, expire)
    timer.daemon = True
    token = _native_emit.set(write)
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        _native_emit.reset(token)


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
