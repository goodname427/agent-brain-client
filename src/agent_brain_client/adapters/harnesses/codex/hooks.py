"""Codex Hook policy, native payload parsing and normalized capture entry."""

import hashlib
import re

from agent_brain_client.adapters.platforms.filesystem import cache_locked


from agent_brain_client.adapters.storage.event_policy import validate_hook_policy as validate_hook_policy, hook_enabled, normalized_hook_fields


def native_hook_fields(policy, payload):
    return normalized_hook_fields(policy, payload)


def stage_hook_messages(queue, scope, turn, kind, text, payload, *, native_home, exclude, excluded):
    """Keep the first prompt's request and stage later native IDs at either Hook."""
    from agent_brain_client.domain.scopes import digest

    source = {key: scope[key] for key in ("project_id", "session_id")}
    key = digest([scope, turn])
    name = "codex-rollouts/" + key + ".json"
    completion = "codex-completions/" + key + ".json"
    with queue._outbox_lock(queue.cache / "codex-rollouts" / (key + ".lock"), "codex-rollout"):
        if excluded():
            return {"status": "excluded-turn", "data_transferred": False}
        result = _stage_hook_messages(
            queue, name, completion, scope, source, turn, kind, text, payload,
            native_home=native_home, exclude=exclude,
        )
        if (result["status"] in ("excluded-turn", "excluded-local-only-or-sensitive")
                and queue._load(completion) is not None):
            queue._discard_staging(completion)
        return result


def resume_completions(queue, submit, *, allowed):
    """Recheck only Stop payloads durably observed behind a local capture gate."""
    from agent_brain_client.domain.errors import CacheBlocked
    from agent_brain_client.domain.scopes import digest

    directory = queue.cache / "codex-completions"
    try:
        queue._safe(directory, queue.cache)
    except CacheBlocked:
        return False
    remaining = False
    for file in sorted(directory.glob("*.json")):
        if not re.fullmatch(r"[a-f0-9]{64}\.json", file.name):
            continue
        name = "codex-completions/" + file.name
        try:
            row = queue._load(name)
            if row is None:
                continue  # A concurrent native Hook completed this request.
            scope, payload = row["scope"], row["payload"]
            key = digest([scope, payload["turn_id"]])
            if (row["schema"] != 1 or file.stem != key or payload["hook_event_name"] != "Stop"
                    or (scope["cwd"], scope["session_id"]) != (payload["cwd"], payload["session_id"])):
                raise ValueError("Codex completion identity changed")
            if not allowed(scope):
                continue
            result = submit(payload)
            if result["status"] in ("excluded-turn", "excluded-local-only-or-sensitive", "retained-paired-event"):
                with queue._outbox_lock(queue.cache / "codex-rollouts" / (key + ".lock"), "codex-rollout"):
                    if queue._load(name) is not None:
                        queue._discard_staging(name)
            remaining = queue._load(name) is not None or remaining
        except (OSError, ValueError, KeyError, TypeError, CacheBlocked):
            # One unreadable local completion must not starve the normal outbox.
            remaining = True
    return remaining


def _completion(scope, payload, state, snap):
    binding = snap.binding if snap is not None else state["binding"]
    start = state["cursor"] if state["binding"] is not None else (
        binding["pins"][0][0] if binding is not None else None
    )
    captured = {
        key: payload.get(key) for key in (
            "hook_event_name", "session_id", "turn_id", "cwd", "transcript_path",
            "stop_hook_active", "last_assistant_message",
        )
    }
    return {
        "schema": 1, "scope": scope, "payload": captured, "binding": binding,
        "start": start, "scan": snap.end if snap is not None else start, "until": None,
        "forms": state["forms"], "only_forms": state["binding"] is None,
        "first_pending": state["first_pending"],
    }


def _stage_hook_messages(queue, name, completion, scope, source, turn, kind, text, payload, *, native_home, exclude):
    from agent_brain_client.adapters.harnesses.codex import transcript
    from agent_brain_client.application.memory.observations import (
        prompt_fingerprint,
        stage_messages,
    )
    from agent_brain_client.domain.errors import CacheBlocked
    from agent_brain_client.domain.scopes import digest

    def stage(operation_id, phase, body):
        return stage_messages(
            queue, **source, operation_id=operation_id, phase=phase,
            messages=[{"role": "user" if phase == "prompt" else "assistant", "content": body}],
        )

    is_stop = kind == "Stop"
    is_form = not is_stop and transcript.form_reply(text)
    ordinary_prompt = not (is_stop or is_form)
    pending = queue._load(completion)
    state = queue._load(name)
    if state is None:
        state = {
            "schema": 1, "binding": None, "cursor": None,
            "first_pending": False, "forms": {}, "unknown_form": False,
        }
    bound = state["binding"] is not None
    first = None
    if state["first_pending"] or (not bound and ordinary_prompt):
        first = prompt_fingerprint(queue, **source, operation_id=turn)
    first_prompt = ordinary_prompt and first is None and not bound
    first_sha = None
    if ordinary_prompt and first is None and (first_prompt or state["first_pending"]):
        first_sha = digest([{"role": "user", "content": text}])
    retry_first = (state["first_pending"] and first_sha is not None
                   and first_sha == state.get("first_prompt_sha256"))
    diagnostic = {}

    if is_form:
        try:
            answers, _ = transcript.form_answers(text)
        except (ValueError, TypeError, KeyError, RecursionError):
            # A known form with uncertain privacy must survive a later read failure.
            state["unknown_form"] = True
            queue._save(name, state)
        else:
            if transcript.private(answers):
                return exclude()

    if pending is not None:
        if not is_stop:
            # The delivery worker owns this Stop's bounded scan until it settles.
            return {"status": "capture-unconfirmed", "data_transferred": False}
        if pending["scope"] != scope or pending["payload"]["last_assistant_message"] != text:
            raise ValueError("Observed Codex completion changed")
    replaying = pending is not None and pending["until"] is not None
    binding = pending["binding"] if pending is not None else state["binding"]
    cursor = (pending["start"] if replaying else pending["scan"]) if pending is not None else state["cursor"]
    known_forms = pending["forms"] if pending is not None else state["forms"]

    snap = None
    observed = None
    if payload.get("transcript_path"):
        try:
            snap = transcript.snapshot(
                native_home(), payload, binding=binding, cursor=cursor,
                until=pending["until"] if replaying else None, allow_next=pending is not None,
            )
            observed = transcript.user_messages(snap.rows, turn, known_forms)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError, CacheBlocked):
            diagnostic["same_turn_capture"] = "unavailable"
    if observed is not None:
        if observed.private:
            return exclude()
        if observed.unknown_form:
            state["unknown_form"] = True
            queue._save(name, state)
        if snap.more:
            diagnostic["same_turn_capture"] = "bounded-pending"
        elif snap.incomplete or observed.incomplete:
            diagnostic["same_turn_capture"] = "bounded-incomplete"
        elif observed.unmatched_form:
            diagnostic["same_turn_capture"] = "form-call-unverified"

    forms = observed.forms if observed is not None else known_forms
    form_wait = state["unknown_form"] or any(forms.values())
    if is_stop and not replaying and (pending is not None or form_wait or (snap is not None and snap.more)):
        job = pending if pending is not None else _completion(scope, payload, state, snap)
        if observed is not None:
            if job["binding"] is None:
                job = _completion(scope, payload, state, snap)
            job = dict(job, scan=snap.end, forms=forms)
            if not snap.more and not form_wait:
                # Replay from the uncommitted cursor only after the whole
                # bounded sequence is cleared for sharing.
                job["until"] = snap.end
        if job != pending:
            queue._save(completion, job)
        return {"status": "capture-unconfirmed", "data_transferred": False,
                "same_turn_capture": "form-privacy-unverified" if form_wait else "bounded-pending"}
    if form_wait or (replaying and observed is None):
        if (is_form and not bound and observed is not None and not state["unknown_form"]
                and not any(item[4] and item[2] == text for item in observed.messages)):
            # The first Hook can carry a public form envelope before the host
            # writes the identifying user row. Keep the verified pre-submit boundary.
            queue._save(name, dict(state, binding=snap.binding, cursor=snap.end, forms=forms))
            return {"status": "awaiting-native-identity", "data_transferred": False}
        if forms != state["forms"]:
            queue._save(name, dict(state, forms=forms))
        return {"status": "capture-unconfirmed", "data_transferred": False,
                "same_turn_capture": "form-privacy-unverified" if form_wait else "unavailable"}

    result = {"status": "awaiting-native-identity", "data_transferred": False}
    if is_stop and not replaying:
        # Seal the final independently of failures in supplemental staging.
        result = stage(turn, "assistant", text)
    elif first_prompt:
        if observed is not None:
            # Preserve the pre-submit boundary even if staging is interrupted.
            queue._save(name, dict(
                state, binding=snap.binding, cursor=snap.size, first_pending=True, forms=forms,
                first_prompt_sha256=first_sha,
            ))
        result = stage(turn, "prompt", text)
        return dict(result, **diagnostic)
    elif retry_first:
        # Reuse the reserved request and preserve the original native cursor.
        result = stage(turn, "prompt", text)
        first = first_sha
    if observed is None:
        return dict(result, **diagnostic)

    first_pending = pending["first_pending"] if replaying else state["first_pending"]
    if first_pending and first is None:
        first = prompt_fingerprint(queue, **source, operation_id=turn)
    only_forms = pending["only_forms"] if replaying else not bound
    failed = False
    for _, message_id, original, content, message_is_form in observed.messages:
        if only_forms and not (message_is_form and (is_stop or is_form and original == text)):
            continue
        if first_pending and not message_is_form:
            # Only the first ordinary native record after the pre-submit
            # cursor can account for the existing turn-keyed prompt. Rows
            # already present when the Hook began are never matched by text.
            matches_first = digest([{"role": "user", "content": original}]) == first
            first_pending = False
            if matches_first:
                continue
        try:
            saved = stage("codex-user-" + digest([turn, message_id]), "prompt", content)
            if not is_stop and original == text:
                result = saved
        except (OSError, ValueError, KeyError, CacheBlocked) as error:
            failed = True
            diagnostic["same_turn_capture_error_type"] = type(error).__name__
    if replaying:
        if failed or snap.more:
            if not failed:
                queue._save(completion, dict(pending, start=snap.end, first_pending=first_pending))
            return dict(result, status="capture-unconfirmed", **diagnostic)
        result = stage(turn, "assistant", text)
        if bound:
            queue._save(name, dict(state, cursor=snap.end, first_pending=first_pending, forms=forms))
        queue._discard_staging(completion)
    elif not is_stop and not failed and (bound or is_form):
        queue._save(name, dict(
            state, binding=snap.binding, cursor=snap.end,
            first_pending=first_pending, forms=forms,
        ))
    elif forms != state["forms"]:
        # Preserve positive form evidence on failure, even if the cursor stays
        # put so that a retry can re-stage the same native IDs.
        queue._save(name, dict(state, forms=forms))
    return dict(result, **diagnostic)


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
