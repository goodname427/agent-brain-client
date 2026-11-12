"""CodeBuddy host boundary over the installed, approved Agent Brain Client.

The Server device protocol remains the enrolled protocol. No identity is
relabelled, credential copied, history read or second memory engine created.
"""

from agent_brain_client.adapters.storage.bounded_json import digest, unique
import agent_brain_client.adapters.harnesses.hook_output as hook_output
import agent_brain_client.adapters.storage.metadata as metadata_io
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
    return core.build_delivery_queue(value, local, "codebuddy")


def capture_messages(core, value, local, payload, messages, phase):
    from agent_brain_client.application.memory.observations import stage_messages
    from agent_brain_client.domain.scopes import source_id, namespace, select
    import uuid

    client, queue = open_queue(core, value, local)
    selected = select(value["scope"], local)
    project = namespace(client.actor, source_id(value["scope"], local, selected["sharing"]))
    generation = payload.get("generation_id") or uuid.uuid4().hex
    if not isinstance(generation, str) or not generation or len(generation) > 1024:
        raise ValueError("CodeBuddy generation identity invalid")
    metadata_io.write(queue.cache / "origin.json", local, cache=True)
    result = stage_messages(queue, project_id=project, session_id=session_id(payload["session_id"]),
                            operation_id="native_" + digest(generation.encode()), phase=phase, messages=messages)
    # Deferred final readers also enter here outside the foreground lifecycle.
    if result.get("local_queue", {}).get("saved") and phase == "assistant":
        try:
            core.schedule_delivery(value["device_home"], "codebuddy", local)
        except Exception:
            pass  # Durable queue survives; the supervisor resumes scheduling.
    return result


def hook(home, stream, *, core=None, defer=None, observe=None):
    from time import monotonic
    started = monotonic()
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
    from agent_brain_client.adapters.storage.project_settings import source_active
    if not source_active(home, payload.get("cwd"), "codebuddy"):
        return {}, {"status": "retained-project-source-stopped", "data_transferred": False}
    local = core.eligible(value, payload.get("cwd"))
    if local is None:
        return {}, {"status": "outside-approved-source", "data_transferred": False}
    # A bound project has its own pinned Hook and queue. Its presence is scoped
    # to this host, so another host's project controls never suppress capture.
    from agent_brain_client.adapters.storage.project_settings import project_settings, load as load_project
    project_file = project_settings(home, payload["cwd"], harness="codebuddy")
    if project_file.exists():
        project = load_project(project_file, metadata_io.digest(project_file.read_bytes()))
        if project["capture_policy"] is not None:
            return {}, {"status": "project-source-delegated", "data_transferred": False}
    session = session_id(payload.get("session_id"))
    kind = payload["hook_event_name"]
    import agent_brain_client.adapters.harnesses.codebuddy.transcript as transcript
    from agent_brain_client.application.memory.lifecycle import handle
    import uuid

    def stage():
        if kind == "Stop":
            if payload.get("stop_hook_active") is not False:
                return {"status": "recursive-stop-skipped", "data_transferred": False}
            try:
                return finish_assistant(home, core, value, local, payload)
            except transcript.PendingTranscript:
                if defer is None:
                    raise
                file = transcript.marker(home, payload)
                from agent_brain_client.adapters.platforms.filesystem import cache_locked
                from agent_brain_client.domain.scopes import digest as scope_digest
                with cache_locked(file.with_suffix(".lock"), timeout=1):
                    row = metadata_io.private_json(file, cache=True)
                    if row["complete"] or row["excluded"]:
                        return {"status": "assistant-already-collected", "data_transferred": False}
                    if (not row.get("stopped") or not row["binding"].get("generation_id")
                            or Path(row["binding"].get("path") or "").name != "index.json"):
                        raise
                    row["completion_scope_sha256"] = scope_digest(value["scope"])
                    metadata_io.write(file, row, cache=True)
                defer(file.stem)
                return {"status": "assistant-capture-deferred", "data_transferred": False}
        native_status = "unavailable"
        try:
            native_status = transcript.observe(home, payload, core)
        except Exception:
            pass
        result = capture_messages(core, value, local, payload,
                                  [{"role": "user", "content": payload["prompt"]}], "prompt")
        return dict(result, assistant_capture=native_status)

    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str):
        raise ValueError("CodeBuddy prompt text required")
    normalized = dict(payload, session_id=session,
                      turn_id=payload.get("generation_id") or uuid.uuid4().hex)
    initialized = monotonic()
    items, diagnostic = handle(
        kind, prompt=prompt, stage=stage,
        schedule=lambda: core.schedule_delivery(home, "codebuddy", local),
        recall=lambda query: core.recall_prompt(value, local, normalized, query,
            **({"observe": observe} if observe is not None else {})),
    )
    diagnostic["timing_ms"]["initialization_ms"] = round((initialized - started) * 1000, 3)
    diagnostic["timing_ms"]["adapter_total_ms"] = round((monotonic() - started) * 1000, 3)
    return hook_output.hook_context(kind, items), diagnostic


def finish_assistant(home, core, value, local, payload):
    import agent_brain_client.adapters.harnesses.codebuddy.transcript as transcript
    from agent_brain_client.adapters.platforms.filesystem import cache_locked

    file = transcript.marker(home, payload)
    from agent_brain_client.application.control import check_request

    with cache_locked(file.with_suffix(".lock"), timeout=1, cancel_check=check_request):
        return _finish_assistant(home, core, value, local, payload)


def _finish_assistant(home, core, value, local, payload):
    import agent_brain_client.adapters.harnesses.codebuddy.transcript as transcript

    try:
        messages, row, file = transcript.assistant(home, payload, core)
    except (FileNotFoundError, json.JSONDecodeError) as error:
        # Only native-read lag is retryable. Never retry these errors if they
        # arise later in queue persistence or a possibly delivered request.
        raise transcript.PendingTranscript("Native transcript not yet readable") from error
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


def resume_observed_assistant(home, core, turn_id):
    """One shared-worker attempt of one observed Stop; never inspect history."""
    import re
    from agent_brain_client.domain.scopes import digest as scope_digest
    import agent_brain_client.adapters.harnesses.codebuddy.transcript as transcript

    if not re.fullmatch(r"[a-f0-9]{64}", turn_id):
        raise ValueError("Exact observed turn identifier required")
    file = Path(home) / ".config/agent-brain-client/codebuddy/turns" / (turn_id + ".json")
    row = metadata_io.private_json(file, cache=True)
    binding = row["binding"]
    if transcript.marker(home, binding) != file:
        raise ValueError("Observed turn binding changed")
    if row["complete"] or row["excluded"] or not row.get("stopped"):
        return False
    value = core.load(home)
    from agent_brain_client.adapters.storage.project_settings import source_active
    if not source_active(home, binding["cwd"], "codebuddy"):
        return True
    local = core.eligible(value, binding["cwd"])
    if (not core.approved(value) or local is None
            or row.get("completion_scope_sha256", scope_digest(value["scope"])) != scope_digest(value["scope"])):
        return True  # Retain paused work, without reading or sending its content.
    native = dict(binding, hook_event_name="Stop", stop_hook_active=False,
                  transcript_path=binding["path"])
    try:
        finish_assistant(home, core, value, local, native)
    except transcript.PendingTranscript:
        return True
    return False
