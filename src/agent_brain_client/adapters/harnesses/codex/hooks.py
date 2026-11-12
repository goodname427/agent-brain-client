"""Codex Hook policy, native payload parsing and normalized capture entry."""

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
