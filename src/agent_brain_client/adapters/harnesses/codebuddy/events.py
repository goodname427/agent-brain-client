"""CodeBuddy host boundary over the installed, approved Agent Brain Client.

The Server device protocol remains the enrolled protocol. No identity is
relabelled, credential copied, history read or second memory engine created.
"""

from agent_brain_client.adapters.storage.bounded_json import digest, unique
import agent_brain_client.adapters.harnesses.hook_output as hook_output
import agent_brain_client.adapters.storage.metadata as metadata_io
import agent_brain_client.domain.privacy as privacy
import agent_brain_client.adapters.storage.registration as registration_store
import json
from pathlib import Path


def session_id(value):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("CodeBuddy session identity required")
    return "codebuddy-" + digest(value.encode())[:48]


def admission(core, value, local, client, session):
    from agent_brain_client.domain.scopes import digest as scope_digest, select

    current = core.load(value["device_home"])
    return (
        core.approved(current)
        and scope_digest(current["scope"]) == scope_digest(value["scope"])
        and select(current["scope"], local) is not None
        and client.status(session).get("source_admitted") is True
    )


def open_queue(core, value, local):
    """Reuse the shared outbox with a distinct CodeBuddy binding and cache."""
    from agent_brain_client.application.queue.outbox import DurableOutbox
    from agent_brain_client.domain.errors import CacheBlocked
    from agent_brain_client.domain.scopes import digest as scope_digest
    from agent_brain_client.adapters.storage.content import FileEventQueue

    client = core.CollectionClient(value, local)
    client.queue_identity = dict(client.queue_identity, host_adapter="codebuddy")
    _, registered = registration_store.registration(value["device_home"])
    runtime = registered["runtime"]
    policy = metadata_io.private_json(
        runtime["content_policy"], runtime["content_policy_sha256"]
    )
    cache = (
        Path(value["device_home"])
        / ".config/agent-brain-client/codebuddy/outboxes"
        / scope_digest(local)
    )
    native = Path(runtime["codex_home"]).expanduser()
    if not native.is_absolute() or native.is_symlink():
        raise CacheBlocked()
    queue = FileEventQueue(
        value["device_home"],
        cache,
        policy,
        client,
        binding={
            "device_home": str(Path(value["device_home"]).resolve()),
            "codex_home": str(native.resolve()),
        },
        protected=[
            Path(value["device_home"]) / name
            for name in (".codex", ".agents", ".config/agent-brain-client/content")
        ],
    )
    if not isinstance(queue, DurableOutbox):
        raise ValueError("Shared Client 0.7 outbox required")
    return client, queue


def replay(core, value, local):
    """Retry one exact pending event per Hook; retain the rest for later Hooks."""
    client, queue = open_queue(core, value, local)
    directory = queue.cache / "outbox"
    if not directory.exists():
        return
    for file in sorted(directory.glob("codebuddy_event_*.json")):
        row = queue._load("outbox/" + file.name)
        if row["state"] != "pending":
            continue
        request = row["request"]
        session = request["params"]["event"]["session_id"]
        queue.invoke(
            request, event_gate=lambda: admission(core, value, local, client, session)
        )
        return


def capture_messages(core, value, local, payload, messages, phase):
    """Split long native text to the existing event contract without truncation."""
    from agent_brain_client.domain.scopes import restricted

    if any(
        privacy.private_content(m["content"]) or restricted(m["content"])
        for m in messages
    ):
        return {"status": "excluded-local-only-or-sensitive", "data_transferred": False}
    import uuid

    native = dict(
        payload, generation_id=payload.get("generation_id") or uuid.uuid4().hex
    )
    batches = []
    batch = []
    for message in messages:
        text = message["content"]
        # At most 8192 UTF-16 units per shared-protocol message, including emoji.
        for start in range(0, len(text), 4096):
            item = {"role": message["role"], "content": text[start : start + 4096]}
            if (
                len(json.dumps([*batch, item], ensure_ascii=False).encode()) > 64000
                or len(batch) == 100
            ):
                batches.append(batch)
                batch = []
            batch.append(item)
    if batch:
        batches.append(batch)
    if not batches:
        return {"status": "empty-native-text", "data_transferred": False}
    # Save every exact batch before starting any network operation. A timeout
    # or killed host cannot lose the tail of a long assistant reply.
    for index, batch in enumerate(batches):
        capture(
            core,
            value,
            local,
            native,
            messages=batch,
            phase=phase + "-" + str(index),
            send=False,
        )
    results = [
        capture(
            core, value, local, native, messages=batch, phase=phase + "-" + str(index)
        )
        for index, batch in enumerate(batches)
    ]
    confirmed = all(
        row.get("local_queue", {}).get("state") == "confirmed" for row in results
    )
    return dict(
        results[-1],
        local_queue=dict(
            results[-1].get("local_queue", {}),
            state="confirmed" if confirmed else "pending",
        ),
        native_event_batches=len(results),
    )


def capture(core, value, local, payload, *, messages=None, phase="prompt", send=True):
    """Capture actual observed text with the existing global outbox/receipt."""
    from datetime import datetime, timezone
    from agent_brain_client.adapters.platforms.filesystem import cache_locked as locked
    from agent_brain_client.domain.scopes import (
        source_id,
        namespace,
        select,
        restricted,
    )
    import agent_brain_client.domain.contract as contract

    messages = (
        messages
        if messages is not None
        else [{"role": "user", "content": payload["prompt"]}]
    )
    for message in messages:
        contract.text(message["content"], 32768)
    if any(
        privacy.private_content(message["content"]) or restricted(message["content"])
        for message in messages
    ):
        return {"status": "excluded-local-only-or-sensitive", "data_transferred": False}
    session = session_id(payload["session_id"])
    client, queue = open_queue(core, value, local)
    selected = select(value["scope"], local)
    sid = source_id(value["scope"], local, selected["sharing"])
    generation = payload.get("generation_id")
    if generation is not None and (
        not isinstance(generation, str) or not generation or len(generation) > 1024
    ):
        raise ValueError("CodeBuddy generation identity invalid")
    # IDE versions without a generation_id still capture each real submission.
    # Do not collapse two identical messages into one event.
    import uuid

    key = digest(
        json.dumps(
            [session, generation or uuid.uuid4().hex, phase], separators=(",", ":")
        ).encode()
    )
    with locked(queue.cache / "codebuddy-events.lock"):
        request_id = "codebuddy_event_" + key
        staged = queue._load("outbox/" + request_id + ".json")
        if staged is not None:
            request = staged["request"]
            if request["params"]["event"]["messages"] != messages:
                raise ValueError(
                    "CodeBuddy generation changed; preserve original event"
                )
        else:
            event = {
                "contract": "agentbrain.event.v1",
                "event_id": "evt-codebuddy-" + key[:48],
                "project_id": namespace(client.actor, sid),
                "session_id": session,
                "occurred_at": datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "messages": messages,
            }
            request = {
                "request_id": request_id,
                "method": "memory_event_submit",
                "params": {"kind": "memory", "id": event["event_id"], "event": event},
            }
            contract.validate(request["method"], request["params"])
        metadata_io.write(queue.cache / "origin.json", local, cache=True)

        def admitted():
            # Reload settings at the transfer boundary so a local stop or scope
            # change during a slow network call cannot admit this queued event.
            return admission(core, value, local, client, session)

        row = queue._enqueue(request)
        if not send:
            return row
        return queue.invoke(request, event_gate=admitted)


def hook(home, stream, *, core=None):
    core = core or (_ for _ in ()).throw(
        ValueError("Verified runtime binding required")
    )
    value = core.load(home)
    # Check local admission before accepting any prompt body from the host.
    if not core.approved(value):
        return {}, {"status": "source-approval-required", "data_transferred": False}
    raw = stream.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("CodeBuddy hook input exceeds bound")
    payload = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(payload, dict) or payload.get("hook_event_name") not in (
        "SessionStart",
        "UserPromptSubmit",
        "Stop",
    ):
        raise ValueError("Unsupported CodeBuddy hook event")
    local = core.eligible(value, payload.get("cwd"))
    if local is None:
        return {}, {"status": "outside-approved-source", "data_transferred": False}
    session = session_id(payload.get("session_id"))
    kind = payload["hook_event_name"]
    diagnostic = {"status": "recall-only", "data_transferred": False}
    try:
        replay(core, value, local)
    except Exception:
        pass  # Pending evidence remains; current capture/recall stay usable.
    if kind != "Stop":
        directory = Path(home) / ".config/agent-brain-client/codebuddy/turns"
        for file in directory.glob("*.json"):
            try:
                row = metadata_io.private_json(file, cache=True)
                binding = row["binding"]
                if (
                    row["complete"]
                    or not row.get("stopped")
                    or binding["cwd"] != payload["cwd"]
                    or not binding["generation_id"]
                ):
                    continue
                native = dict(
                    binding,
                    hook_event_name="Stop",
                    stop_hook_active=False,
                    transcript_path=binding["path"],
                )
                finish_assistant(home, core, value, local, native)
            except Exception:
                continue  # An unavailable turn must not starve other pending turns.
            break
    if kind == "Stop":
        if payload.get("stop_hook_active") is not False:
            return {}, {"status": "recursive-stop-skipped", "data_transferred": False}
        return {}, finish_assistant(home, core, value, local, payload)
    # Failure to capture must not remove independently available recall.
    if kind == "UserPromptSubmit":
        native_status = "unavailable"
        try:
            import agent_brain_client.adapters.harnesses.codebuddy.transcript as transcript

            native_status = transcript.observe(home, payload, core)
        except Exception:
            pass  # Prompt capture and recall remain independently available.
        try:
            diagnostic = capture_messages(
                core,
                value,
                local,
                payload,
                [{"role": "user", "content": payload["prompt"]}],
                "prompt",
            )
            diagnostic["assistant_capture"] = native_status
        except Exception as error:
            diagnostic = {
                "status": "capture-unconfirmed",
                "error_type": type(error).__name__,
                "preserve_existing_state": True,
            }
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str):
        raise ValueError("CodeBuddy prompt text required")
    from agent_brain_client.domain.scopes import restricted

    if privacy.private_content(prompt) or restricted(prompt):
        return {}, diagnostic
    query = prompt[:1024].strip() or "personal preferences and task progress"
    try:
        items = core.recall(value, local, session, query)
        return hook_output.hook_context(kind, items), diagnostic
    except Exception as error:
        return {}, dict(
            diagnostic,
            recall_status="unavailable",
            recall_error_type=type(error).__name__,
        )


def finish_assistant(home, core, value, local, payload):
    import agent_brain_client.adapters.harnesses.codebuddy.transcript as transcript

    messages, row, file = transcript.assistant(home, payload, core)
    if not messages:
        if row["excluded"] and not row["complete"]:
            row["complete"] = True
            metadata_io.write(file, row, cache=True)
        return {
            "status": "assistant-text-unavailable-or-already-collected",
            "data_transferred": False,
        }
    # Bind retries without generation IDs to the observed prompt cursor.
    native = dict(
        payload,
        generation_id=payload.get("generation_id")
        or digest(json.dumps(row, sort_keys=True).encode()),
    )
    result = capture_messages(core, value, local, native, messages, "assistant")
    if result.get("local_queue", {}).get("saved"):
        # All batches were already saved before transfer. "complete" marks
        # native collection only; the outbox/Server receipt owns save status.
        row["complete"] = True
        metadata_io.write(file, row, cache=True)
    return result
