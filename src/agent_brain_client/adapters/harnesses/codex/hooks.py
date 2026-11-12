"""Codex Hook policy, native payload parsing and normalized capture entry."""

import hashlib

from agent_brain_client.adapters.platforms.filesystem import cache_locked


from agent_brain_client.adapters.storage.event_policy import validate_hook_policy as validate_hook_policy, hook_enabled, normalized_hook_fields


def native_hook_fields(policy, payload):
    return normalized_hook_fields(policy, payload)


def run_event_hook(adapter, policy, payload, *, clock=None, source_gate=None):
    """One native hook through the official Client outbox; no history reads.

    The caller validates the pinned policy before reading stdin and activating
    the credential store. Tests inject only the ordinary public Client classes.
    """
    if not hook_enabled(policy):
        return {"status": "disabled", "data_transferred": False}
    if policy.get("schema") == 2 and not callable(source_gate):
        raise ValueError("Persistent capture requires the managed source entry")
    if (
        getattr(adapter.client, "profile_sha", policy["profile_sha256"])
        != policy["profile_sha256"]
    ):
        raise ValueError("Hook Client destination changed")
    scope, turn, kind, content, key = native_hook_fields(policy, payload)
    from agent_brain_client.application.memory.capture import submit_turn

    return submit_turn(
        adapter,
        scope,
        turn,
        "prompt" if kind == "UserPromptSubmit" else "final",
        content,
        enabled=lambda: hook_enabled(policy),
        lock=cache_locked,
        clock=clock,
        source_gate=source_gate,
    )


# Codex invokes notify once after a turn and passes the event as one JSON argv.
# An internal marker makes the queued capture turn a one-shot, not a loop.
_NOTIFY_CAPTURE_MARKER = "AGENTBRAIN_NOTIFY_CAPTURE:"
_NOTIFY_CAPTURE_INSTRUCTIONS = (
    "Review the immediately preceding completed Codex turn for user-stated preferences, "
    "facts, experiences, events, corrections, or short-term progress. Do not filter "
    "these out solely because they are ordinary, temporary, or not important. Use only "
    "direct user statements as evidence; ignore assistant "
    "guesses and tool output. Process only the supported, authorized Codex Memories "
    "scope exposed by content_sources_inventory/read/review/stage. Read every "
    "eligible source to completion in bounded pages; do not claim account-wide or "
    "session-history coverage. Do not count unread items as complete. Generalize "
    "ordinary user project experience safely for the shared personal Memory scope; "
    "exclude secrets, LocalOnly, confidential or customer-identifying content, and "
    "report unsafe scope as deferred. For memory writes use content_read/CAS and "
    "content_request_id with the supplied turn digest. Honor explicit forgetting "
    "with the existing authorized content_remove path. Do not read transcripts or "
    "store raw conversation text. Never claim durable save without the Server receipt."
)


def codex_notify_capture(event):
    """Validate the documented notify payload and prepare one capture message."""
    if not isinstance(event, dict) or event.get("type") != "agent-turn-complete":
        return None
    thread_id = event.get("thread-id")
    turn_id = event.get("turn-id")
    messages = event.get("input-messages")
    if (
        not isinstance(thread_id, str)
        or not thread_id
        or len(thread_id) > 128
        or not isinstance(turn_id, str)
        or not turn_id
        or len(turn_id) > 256
        or not isinstance(messages, list)
        or not messages
        or len(messages) > 128
    ):
        return None
    if any(not isinstance(item, str) or len(item) > 65536 for item in messages):
        return None
    if any(item.startswith(_NOTIFY_CAPTURE_MARKER) for item in messages):
        return None
    last = event.get("last-assistant-message", "")
    if not isinstance(last, str) or len(last) > 65536:
        return None
    turn_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    message = (
        _NOTIFY_CAPTURE_MARKER
        + turn_id
        + "\nTurn digest: "
        + turn_digest
        + "\n"
        + _NOTIFY_CAPTURE_INSTRUCTIONS
    )
    return {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "turn_sha256": turn_digest,
        "message": message,
        "input_message_count": len(messages),
    }
