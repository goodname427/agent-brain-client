"""Codex native Hook parsing, bounded files, source routing and local delivery."""

import sys
import json
import os
from pathlib import Path
from agent_brain_client.domain.scopes import (
    select,
    source_id,
    namespace,
    digest,
    ordinary_drive_path,
)
from agent_brain_client.adapters.storage import (
    registration as registration_store,
    metadata as metadata_io,
)
from agent_brain_client.adapters.harnesses.hook_output import hook_context

ROOT = Path(__file__).resolve().parents[5]


def submit_hook(value, local, payload, *, services):
    from agent_brain_client.adapters.harnesses.codex.hooks import (
        native_hook_fields,
        stage_hook_messages,
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
    scope, turn, kind, text, key = native_hook_fields(cap, payload)
    blocked = directory / ("excluded-" + key + ".json")
    if blocked.exists():
        return {"status": "excluded-turn", "data_transferred": False}

    _, queue = services.adapter(value, local)
    previous = queue._load("outbox/agentbrain_event_" + key + ".json")
    if previous is not None:
        return dict(queue._queue_result(previous), status="retained-paired-event")
    return stage_hook_messages(
        queue, scope, turn, kind, text, payload, excluded=blocked.exists,
        native_home=lambda: registration_store.content_location(value["device_home"], r, "codex")[0],
    )


def hook(value, *, services, observe=None):
    from time import monotonic
    started = monotonic()
    local = services.eligible(value, os.getcwd(), prompt=True)
    if local is None or services.legacy(value, local) is not None:
        return {}
    from agent_brain_client.adapters.storage.project_settings import source_active
    if not source_active(value["device_home"], local["cwd"], "codex"):
        return {}
    raw = sys.stdin.buffer.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("Bounded native payload required")
    payload = json.loads(raw, object_pairs_hook=metadata_io.unique)
    if ordinary_drive_path(payload.get("cwd")) != local["cwd"]:
        return {}
    payload["cwd"] = local["cwd"]
    kind = payload.get("hook_event_name")
    from agent_brain_client.application.memory.lifecycle import handle

    scope = value["scope"]
    capture = select(scope, local) is not None
    if not capture and kind != "UserPromptSubmit":
        return {}
    recall = kind == "UserPromptSubmit" and select(scope, local, recall=True) is not None
    initialized = monotonic()
    items, diagnostic = handle(
        kind, prompt=payload.get("prompt", ""),
        stage=(lambda: services.submit_hook(value, local, payload)) if capture else None,
        schedule=(lambda: services.schedule_delivery(value["device_home"], "codex", local)) if capture else None,
        recall=(lambda query: services.recall_prompt(value, local, payload, query, observe=observe)) if recall else None,
    )
    diagnostic["timing_ms"]["initialization_ms"] = round((initialized - started) * 1000, 3)
    diagnostic["timing_ms"]["adapter_total_ms"] = round((monotonic() - started) * 1000, 3)
    print(json.dumps(dict(operation="native-lifecycle", harness="codex", **diagnostic)), file=sys.stderr)
    return hook_context(kind, items)
