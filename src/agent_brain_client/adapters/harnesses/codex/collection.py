"""Codex native Hook parsing, bounded files, source routing and local delivery."""

import sys
import json
import os
from pathlib import Path
from agent_brain_client.domain import privacy
from agent_brain_client.domain.scopes import (
    select,
    source_id,
    namespace,
    digest,
    restricted,
)
from agent_brain_client.adapters.storage import (
    registration as registration_store,
    metadata as metadata_io,
)
from agent_brain_client.adapters.storage.recall_receipts import (
    recall_observation,
)
from agent_brain_client.adapters.harnesses.hook_output import hook_context
from agent_brain_client.adapters.platforms.filesystem import cache_directory

ROOT = Path(__file__).resolve().parents[5]


def adapter(value, local, *, services):
    from agent_brain_client.adapters.harnesses.codex.content import CodexContentSession
    from agent_brain_client.adapters.storage.content import operation_stage

    _, registered = registration_store.registration(value["device_home"])
    r = registered["runtime"]
    client = services.CollectionClient(value, local)
    policy = metadata_io.private_json(r["content_policy"], r["content_policy_sha256"])
    cache = (
        services.settings_path(value["device_home"]).parent / "outboxes" / digest(local)
    )
    with operation_stage("global-cache-open"):
        cache_directory(cache)
        return client, CodexContentSession(
            value["device_home"], str(cache), policy, client, codex_home=r["codex_home"]
        )


def submit_hook(value, local, payload, *, services):
    from agent_brain_client.adapters.harnesses.codex.hooks import (
        native_hook_fields,
    )

    selected = select(value["scope"], local)
    _, registered = registration_store.registration(value["device_home"])
    r = registered["runtime"]
    profile = metadata_io.private_json(r["https_profile"], r["https_profile_sha256"])
    sid = source_id(value["scope"], local, selected["sharing"])
    directory = (
        services.settings_path(value["device_home"]).parent / "outboxes" / digest(local)
    )
    cap = {
        "schema": 2,
        "enabled": True,
        "lifecycle": "until-disabled",
        "source_mode": "codex-project",
        "notes_directory": None,
        "profile_sha256": r["https_profile_sha256"],
        "content_policy_sha256": r["content_policy_sha256"],
        "off_file": str(
            services.settings_path(value["device_home"]).parent / "global.off"
        ),
        "scopes": [
            {
                "cwd": local["cwd"],
                "project_id": namespace(profile["grant"]["actor"], sid),
                "session_id": None,
            }
        ],
    }
    _, _, kind, text, key = native_hook_fields(cap, payload)
    blocked = directory / ("excluded-" + key + ".json")
    if blocked.exists():
        return {"status": "excluded-turn", "data_transferred": False}
    file = directory / "hook-turns" / (key + ".json")
    staged = metadata_io.private_json(file, cache=True) if file.exists() else None
    if (
        (privacy.private_content(text) or restricted(text))
        or isinstance(staged, dict)
        and (
            privacy.private_content(staged.get("prompt", ""))
            or restricted(staged.get("prompt", ""))
        )
    ):
        cache_directory(directory)
        metadata_io.write(
            blocked,
            {"schema": 1, "reason": "local-only-or-sensitive"},
            exclusive=True,
            cache=True,
        )
        return {"status": "excluded-local-only-or-sensitive", "data_transferred": False}
    client, queue = services.adapter(value, local)
    metadata_io.write(queue.cache / "origin.json", local, cache=True)
    from agent_brain_client.application.memory.observations import stage_messages

    scope, turn, _, text, _ = native_hook_fields(cap, payload)
    previous = queue._load("outbox/agentbrain_event_" + key + ".json")
    if previous is not None:
        return dict(queue._queue_result(previous), status="retained-paired-event")
    return stage_messages(
        queue, project_id=scope["project_id"], session_id=scope["session_id"],
        operation_id=turn, phase="prompt" if kind == "UserPromptSubmit" else "assistant",
        messages=[{"role": "user" if kind == "UserPromptSubmit" else "assistant", "content": text}],
    )


def hook(value, *, services, observe=None):
    from time import monotonic
    started = monotonic()
    local = services.eligible(value, os.getcwd())
    if local is None:
        return {}
    raw = sys.stdin.buffer.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("Bounded native payload required")
    payload = json.loads(raw, object_pairs_hook=metadata_io.unique)
    if payload.get("cwd") != local["cwd"]:
        return {}
    kind = payload.get("hook_event_name")
    if services.legacy(value, local) is not None:
        return {}
    from agent_brain_client.application.memory.lifecycle import handle

    initialized = monotonic()
    items, diagnostic = handle(
        kind, prompt=payload.get("prompt", ""),
        stage=lambda: services.submit_hook(value, local, payload),
        schedule=lambda: services.schedule_delivery(value["device_home"], "codex", local),
        recall=lambda query: services.recall_prompt(value, local, payload, query, observe=observe),
    )
    diagnostic["timing_ms"]["initialization_ms"] = round((initialized - started) * 1000, 3)
    diagnostic["timing_ms"]["adapter_total_ms"] = round((monotonic() - started) * 1000, 3)
    print(json.dumps(dict(operation="native-lifecycle", harness="codex", **diagnostic)), file=sys.stderr)
    return hook_context(kind, items)


def flush(value, *, services):
    root = services.settings_path(value["device_home"]).parent / "outboxes"
    results = []
    if not root.exists():
        return {"status": "no-global-outbox"}
    for directory in root.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        local = metadata_io.private_json(directory / "origin.json", cache=True)
        if not services.approved(value) or select(value["scope"], local) is None:
            continue
        client, queue = services.adapter(value, local)
        outbox = queue.cache / "outbox"
        if not outbox.exists():
            continue
        for file in outbox.iterdir():
            row = queue._load("outbox/" + file.name)
            request = row.get("request", {}) if isinstance(row, dict) else {}
            event = request.get("params", {}).get("event")
            if (
                not isinstance(event, dict)
                or request.get("method") != "memory_event_submit"
            ):
                continue
            if any(
                (
                    privacy.private_content(m.get("content", ""))
                    or restricted(m.get("content", ""))
                )
                for m in event.get("messages", [])
            ):
                continue
            results.append(
                queue.invoke(
                    request,
                    event_gate=lambda: services.approved(value)
                    and client.status(event["session_id"]).get("source_admitted")
                    is True,
                )
            )
    return {
        "status": "global-original-queue-sync",
        "results": results,
        "model_requests_reissued_directly": False,
    }


def status(
    value,
    *,
    check_server=False,
    request_key=None,
    event_id=None,
    session_id=None,
    services,
):
    if event_id is not None or session_id is not None:
        from agent_brain_client.domain.events import event_identifier

        event_identifier(event_id)
        event_identifier(session_id)
        local = services.eligible(value, os.getcwd())
        if local is None:
            raise ValueError("Current source origin not approved")
        # Exact event diagnostics must not first call the admission-only status.
        return {
            "event_id": event_id,
            "session_id": session_id,
            "server_event_status": services.CollectionClient(value, local).status(
                session_id, event_id=event_id
            ),
            "credentials_accessed": True,
        }
    from agent_brain_client.adapters.harnesses.codex.collection_installation import (
        installation_status,
    )

    installed = installation_status(value)
    result = {
        "global_entry_installed": installed,
        "expanded_sources_enabled": installed and services.approved(value),
        "admission": services.admission(value),
        "recall_observation": recall_observation(value, request_key),
        "scope": value["scope"],
        "legacy_project_roots": [r["root"] for r in value["legacy_projects"]],
        "cloud_orchestration_supported": False,
        "native_delivery": "unobserved",
        "credentials_accessed": False,
        "default_sharing": "cross-project",
        "applicability": "origin-and-current-question",
        "legacy_capture_migrated": [
            r["root"]
            for r in value["legacy_projects"]
            if not r.get("capture_delegated", True)
        ],
    }
    try:
        code_matches = all(
            metadata_io.digest((ROOT / name).read_bytes()) == pin
            for name, pin in value["code_sha256"].items()
        )
    except OSError:
        code_matches = False
    result["checkout_code_matches_hook_pins"] = code_matches
    if not code_matches:
        result["expanded_sources_enabled"] = None
    if check_server:
        result["server_admission"] = services.CollectionClient(value).wire(
            "memory_global_status", {}
        )
        result["credentials_accessed"] = True
    return result
