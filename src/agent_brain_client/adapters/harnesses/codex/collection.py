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
from agent_brain_client.adapters.harnesses.hook_output import hook_context
from agent_brain_client.adapters.platforms.filesystem import cache_directory

ROOT = Path(__file__).resolve().parents[5]


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
    from agent_brain_client.adapters.storage.project_settings import source_active
    if not source_active(value["device_home"], os.getcwd(), "codex"):
        return {}
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
