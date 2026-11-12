"""Shared foreground lifecycle: durable observation, background delivery, recall."""

from time import monotonic
from agent_brain_client.domain import privacy
from agent_brain_client.domain.scopes import restricted


def handle(kind, *, prompt="", stage, schedule, recall):
    if kind not in ("SessionStart", "UserPromptSubmit", "Stop"):
        raise ValueError("Unsupported lifecycle event")
    timings = {}
    started = monotonic()
    diagnostic = {"status": "session-ready", "data_transferred": False}
    if kind != "SessionStart":
        before = monotonic()
        try:
            diagnostic = stage()
        except Exception as error:
            if kind == "Stop":
                raise
            diagnostic = {"status": "capture-unconfirmed", "error_type": type(error).__name__}
        timings["stage_ms"] = round((monotonic() - before) * 1000, 3)
    before = monotonic()
    try:
        schedule()
    except Exception as error:
        diagnostic = dict(diagnostic, delivery_schedule="pending", schedule_error_type=type(error).__name__)
    timings["schedule_ms"] = round((monotonic() - before) * 1000, 3)
    items = []
    if kind == "UserPromptSubmit" and isinstance(prompt, str) and prompt.strip() and not privacy.private_content(prompt) and not restricted(prompt):
        before = monotonic()
        try:
            items = recall(prompt[:1024])
        except Exception as error:
            diagnostic = dict(diagnostic, recall_status="unavailable", recall_error_type=type(error).__name__)
        timings["recall_ms"] = round((monotonic() - before) * 1000, 3)
    timings["total_ms"] = round((monotonic() - started) * 1000, 3)
    return items, dict(diagnostic, timing_ms=timings)
