"""Shared native Hook response dialect used by the supported Harnesses."""

from agent_brain_client.application.memory.context import injectable_context


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
