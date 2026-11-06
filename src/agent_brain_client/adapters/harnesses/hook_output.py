"""Shared native Hook response dialect used by the supported Harnesses."""

from agent_brain_client.application.memory.context import render_context


def hook_context(kind, items):
    if not items:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": kind,
            "additionalContext": render_context(items),
        }
    }
