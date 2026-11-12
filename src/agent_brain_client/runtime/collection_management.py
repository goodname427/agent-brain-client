"""Shared queue synchronization and source status over explicit Harness ports."""

import os
from pathlib import Path
from agent_brain_client.domain import privacy
from agent_brain_client.domain.scopes import select, restricted
from agent_brain_client.adapters.storage import metadata as metadata_io
from agent_brain_client.adapters.storage.recall_receipts import recall_observation

ROOT = Path(__file__).resolve().parents[3]


def queue_root(home, harness):
    return Path(home) / ".config/agent-brain-client" / ("global-memory" if harness == "codex" else harness) / "outboxes"


def flush(value, *, services):
    results, hosts = [], {}
    for harness in ("codex", "codebuddy"):
        try:
            result = flush_host(value, harness, services=services)
        except Exception as error:
            result = {"status": "queue-review-required", "error_type": type(error).__name__, "results": []}
        hosts[harness] = result
        results.extend(result.get("results", []))
    return {"status": "global-original-queue-sync" if all(row["status"] != "queue-review-required" for row in hosts.values()) else "global-queue-sync-incomplete", "hosts": hosts, "results": results,
            "model_requests_reissued_directly": False}


def flush_host(value, harness, *, services):
    root = queue_root(value["device_home"], harness)
    results = []
    if not root.exists():
        return {"status": "no-global-outbox"}
    for directory in root.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        local = metadata_io.private_json(directory / "origin.json", cache=True)
        if not services.approved(value) or select(value["scope"], local) is None:
            continue
        from agent_brain_client.adapters.storage.project_settings import source_active
        if not source_active(value["device_home"], local["cwd"], harness):
            continue
        client, queue = services.build_delivery_queue(value, local, harness)
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
                    event_gate=lambda: source_active(value["device_home"], local["cwd"], harness)
                    and services.approved(value)
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
    hosts = services.host_status(value)
    installed = any(row.get("installed") is True for row in hosts.values())
    result = {
        "global_entry_installed": installed,
        "hosts": hosts,
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
