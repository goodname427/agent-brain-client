"""Codex Hook policy, native payload parsing and normalized capture entry."""

from pathlib import Path
from agent_brain_client.domain import contract
from agent_brain_client.adapters.platforms.filesystem import private_mode, cache_locked


def validate_hook_policy(value, *, profile_sha256, content_policy_sha256):
    """Private exact pin, finite sessions; this does not enroll native threads."""
    fields = {
        "schema",
        "enabled",
        "profile_sha256",
        "content_policy_sha256",
        "off_file",
        "scopes",
    }
    daily = isinstance(value, dict) and value.get("schema") == 2
    if daily:
        if (
            set(value) != fields | {"lifecycle", "source_mode", "notes_directory"}
            or value.get("lifecycle") != "until-disabled"
            or value.get("source_mode") not in ("codex-project", "operator-notes")
        ):
            raise ValueError("Persistent source grant required")
        shape = dict(value, schema=1)
        shape.pop("lifecycle")
        shape.pop("source_mode")
        shape.pop("notes_directory")
    else:
        shape = value
    if (
        not isinstance(value, dict)
        or set(shape) not in (fields, fields | {"not_before", "expires_at"})
        or shape["schema"] != 1
        or type(value["enabled"]) is not bool
    ):
        raise ValueError("Exact hook policy required")
    if "not_before" in value and (
        type(value["not_before"]) is not int
        or type(value["expires_at"]) is not int
        or not 0 < value["expires_at"] - value["not_before"] <= 3600
    ):
        raise ValueError("Finite source window required")
    if (
        value["profile_sha256"] != profile_sha256
        or value["content_policy_sha256"] != content_policy_sha256
    ):
        raise ValueError("Hook destination policy changed")
    off = Path(value["off_file"])
    if (
        not off.is_absolute()
        or ".." in off.parts
        or off.is_symlink()
        or off.parent.resolve() != off.parent
    ):
        raise ValueError("Private absolute off switch required")
    private_mode(off.parent, directory=True, owner=True)
    scopes = value["scopes"]
    if (
        not isinstance(scopes, list)
        or len(scopes) > 16
        or value["enabled"]
        and not scopes
    ):
        raise ValueError("Finite hook scopes required")
    seen = set()
    for scope in scopes:
        if not isinstance(scope, dict) or set(scope) != {
            "cwd",
            "project_id",
            "session_id",
        }:
            raise ValueError("Exact hook scope required")
        contract.event_identifier(scope["project_id"])
        if not (
            daily
            and value["source_mode"] == "codex-project"
            and scope["session_id"] is None
        ):
            contract.event_identifier(scope["session_id"])
        path = Path(scope["cwd"])
        if (
            not path.is_absolute()
            or path.is_symlink()
            or str(path.resolve()) != scope["cwd"]
            or scope["session_id"] in seen
        ):
            raise ValueError("Canonical unique project/session required")
        seen.add(scope["session_id"])
    return value


def hook_enabled(policy):
    # Existence includes symlinks: every off marker fails closed, before stdin.
    off = Path(policy["off_file"])
    import time

    window = (
        "not_before" not in policy
        or policy["not_before"] <= time.time() < policy["expires_at"]
    )
    return (
        policy["enabled"] is True
        and window
        and not off.exists()
        and not off.is_symlink()
    )


def native_hook_fields(policy, payload):
    """Validate text and finite metadata before credential activation."""
    if not isinstance(payload, dict) or payload.get("hook_event_name") not in (
        "UserPromptSubmit",
        "Stop",
    ):
        raise ValueError("Unsupported native hook")
    session = payload.get("session_id")
    turn = payload.get("turn_id")
    contract.event_identifier(session)
    contract.event_identifier(turn)
    scope = next(
        (
            scope
            for scope in policy["scopes"]
            if (
                scope["session_id"] == session
                or policy.get("lifecycle") == "until-disabled"
                and policy.get("source_mode") == "codex-project"
                and scope["session_id"] is None
            )
            and payload.get("cwd") == scope["cwd"]
        ),
        None,
    )
    if scope is None:
        raise ValueError("Unregistered native scope")
    scope = dict(scope, session_id=session)
    kind = payload["hook_event_name"]
    if kind == "Stop" and payload.get("stop_hook_active") is not False:
        raise ValueError("Recursive or unspecified Stop rejected")
    content = payload.get(
        "prompt" if kind == "UserPromptSubmit" else "last_assistant_message"
    )
    from agent_brain_client.domain.turns import turn_key

    # The envelope is bounded by the host reader. Transport message limits
    # apply after shared staging splits the complete observation.
    contract.text(content, 1048576)
    key = turn_key(scope, turn)
    return scope, turn, kind, content, key


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
