"""User-level memory router; new origins require a matching Server approval.

With no expanded approval this exits before reading Hook stdin. Existing owned
project collectors keep their original code, policies and trust definitions.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    _ENTRY_ROOT = Path(__file__).resolve().parents[3]
    sys.path[:0] = [str(_ENTRY_ROOT / "src"), str(_ENTRY_ROOT)]


from agent_brain_client.adapters.storage.collection import (
    settings_path,
    load,
    approved,
    admission_state,
)
from agent_brain_client.adapters.storage.origins import discover
from agent_brain_client.adapters.server.collection import CollectionClient
from agent_brain_client.adapters.storage.recall_receipts import (
    save_recall_observation,
    recall_observation,
)
from agent_brain_client.adapters.harnesses.hook_output import hook_context


import agent_brain_client.domain.privacy as privacy
import agent_brain_client.adapters.storage.registration as registration_store
import agent_brain_client.adapters.storage.metadata as metadata_io

import argparse
import json
import os
import time

ROOT = Path(__file__).resolve().parents[3]
import agent_brain_client.adapters.harnesses.codex.project as project_sources
from agent_brain_client.adapters.platforms.filesystem import cache_directory
from agent_brain_client.domain.scopes import (
    select,
    source_id,
    namespace,
    digest,
    within,
    restricted,
)

MARKER = "Agent Brain global memory"


def resume(value):
    """Resume only an already approved source; preserve fences on uncertainty."""
    legacy_sources = [
        row
        for row in value.get("legacy_projects", [])
        if row.get("capture_delegated", True)
    ]
    if not value["server_scope_sha256"] and legacy_sources:
        # Preserve the original per-project control path; it cannot activate
        # the unapproved expanded scope on this device.
        off = settings_path(value["device_home"]).parent / "global.off"
        off.touch(mode=0o600)
        for row in legacy_sources:
            source = project_sources.load(row["settings"], row["settings_sha256"])
            project_sources.daily_command(source, "resume")
        off.unlink()
        return {
            "status": "global-memory-legacy-resumed",
            "collection_enabled": False,
            "legacy_capture_resumed": True,
            "remote_confirmed": True,
            "next_action": admission_state(value)["next_action"],
        }
    state = admission_state(value)
    if any(reason != "local-stop-fence" for reason in state["reasons"]):
        return {
            "status": "global-memory-activation-required",
            "collection_enabled": False,
            "remote_confirmed": False,
            "admission": state,
            "permissions_changed": False,
        }
    off = settings_path(value["device_home"]).parent / "global.off"
    off.touch(mode=0o600)
    response = CollectionClient(value).wire("memory_global_resume", {})
    result = response.get("result", {})
    if (
        "error" in response
        or result.get("schema") != 1
        or result.get("stopped") is not False
    ):
        return {
            "status": "global-memory-resume-unconfirmed",
            "collection_enabled": False,
            "remote_confirmed": False,
            "server_error": response.get("error"),
            "local_stop_fence": True,
            "next_action": "Inspect global-status --check-server and the existing Server source; preserve the local stop fence.",
        }
    off.unlink()
    return {
        "status": "global-memory-resumed",
        "collection_enabled": True,
        "remote_confirmed": True,
    }


def eligible(value, cwd):
    if not approved(value):
        return None
    # Avoid receiving content for a directory outside every approved area.
    scope = value["scope"]
    if any(
        within(str(Path(cwd).resolve()), denied) for denied in scope["exclude_roots"]
    ):
        return None
    if not scope.get("all_projects") and not any(
        within(str(Path(cwd).resolve()), r)
        for r in [
            *[x["root"] for x in scope["include_roots"]],
            *scope["non_project_roots"],
        ]
    ):
        return None
    local = discover(cwd)
    return local if select(scope, local) is not None else None


def legacy(value, local):
    return next(
        (
            row
            for row in value["legacy_projects"]
            if row["root"] == local["cwd"] and row.get("capture_delegated", True)
        ),
        None,
    )


def adapter(value, local):
    from agent_brain_client.adapters.harnesses.codex.content import CodexContentSession
    from agent_brain_client.adapters.storage.content import operation_stage

    _, registered = registration_store.registration(value["device_home"])
    r = registered["runtime"]
    client = CollectionClient(value, local)
    policy = metadata_io.private_json(r["content_policy"], r["content_policy_sha256"])
    cache = settings_path(value["device_home"]).parent / "outboxes" / digest(local)
    with operation_stage("global-cache-open"):
        cache_directory(cache)
        return client, CodexContentSession(
            value["device_home"], str(cache), policy, client, codex_home=r["codex_home"]
        )


def submit_hook(value, local, payload):
    from agent_brain_client.adapters.harnesses.codex.hooks import (
        native_hook_fields,
        run_event_hook,
    )

    selected = select(value["scope"], local)
    _, registered = registration_store.registration(value["device_home"])
    r = registered["runtime"]
    profile = metadata_io.private_json(r["https_profile"], r["https_profile_sha256"])
    sid = source_id(value["scope"], local, selected["sharing"])
    directory = settings_path(value["device_home"]).parent / "outboxes" / digest(local)
    cap = {
        "schema": 2,
        "enabled": True,
        "lifecycle": "until-disabled",
        "source_mode": "codex-project",
        "notes_directory": None,
        "profile_sha256": r["https_profile_sha256"],
        "content_policy_sha256": r["content_policy_sha256"],
        "off_file": str(settings_path(value["device_home"]).parent / "global.off"),
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
    client, queue = adapter(value, local)
    metadata_io.write(queue.cache / "origin.json", local, cache=True)
    return run_event_hook(
        queue,
        cap,
        payload,
        source_gate=lambda: approved(value)
        and select(value["scope"], local) is not None
        and client.status(payload["session_id"]).get("source_admitted") is True,
    )


def hook(value, *, observe=None):
    local = eligible(value, os.getcwd())
    if local is None:
        return {}
    raw = sys.stdin.buffer.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("Bounded native payload required")
    payload = json.loads(raw, object_pairs_hook=metadata_io.unique)
    if payload.get("cwd") != local["cwd"]:
        return {}
    kind = payload.get("hook_event_name")
    if kind in ("UserPromptSubmit", "Stop"):
        if legacy(value, local) is not None:
            return {}
        from agent_brain_client.adapters.storage.content import operation_stage

        with operation_stage("global-capture"):
            result = submit_hook(value, local, payload)
        if kind != "UserPromptSubmit" or result.get("status") != "staged":
            return {}
        question = payload.get("prompt")
        if (
            not isinstance(question, str)
            or not question.strip()
            or privacy.private_content(question)
            or restricted(question)
        ):
            return {}
        from agent_brain_client.adapters.platforms.filesystem import locked

        directory = settings_path(value["device_home"]).parent
        key = digest(
            [
                digest(value["scope"]),
                local,
                payload.get("session_id"),
                payload.get("turn_id"),
                question,
                *([value["recall"]] if value.get("recall") else []),
            ]
        )
        stamp = directory / ("prompt-" + key + ".json")
        with locked(directory / "prompt-recall.lock"):
            if stamp.exists():
                return {}
            query = question[:1024]
            receipt = {
                "schema": 2,
                "at": time.time(),
                "stage": "agentbrain-global-recall",
                "request_key": key,
                "request_id": "global_recall_" + key,
                "session_sha256": digest(payload["session_id"]),
                "turn_sha256": digest(payload["turn_id"]),
                "query_sha256": digest(query),
                "output_written": False,
                "model_used": "unknown",
                "status": "failed",
                "error_type": None,
                "layer_counts": {"L1": 0, "L2": 0, "L3": 0},
                "item_count": 0,
                "context_bytes": 0,
                "context_sha256": None,
                "offline_cache": False,
                "retrieval": {
                    "candidate_count": 0,
                    "returned": [],
                    "selected": [],
                    "omitted_for_budget": 0,
                },
            }
            try:
                with operation_stage("global-recall"):
                    items = recall(
                        value,
                        local,
                        payload["session_id"],
                        query,
                        request_id=receipt["request_id"],
                        observe=receipt["retrieval"],
                    )
                result = hook_context(kind, items)
            except Exception as error:
                receipt["error_type"] = type(error).__name__
                save_recall_observation(value, receipt)
                if observe is not None:
                    observe(receipt)
                raise
            context = result.get("hookSpecificOutput", {}).get("additionalContext", "")
            receipt.update(
                {
                    "status": "context-prepared" if result else "empty",
                    "layer_counts": {
                        layer: sum(item.get("layer") == layer for item in items)
                        for layer in ("L1", "L2", "L3")
                    },
                    "item_count": len(items),
                    "context_bytes": len(context.encode()),
                    "context_sha256": digest(context),
                    "offline_cache": any(
                        item.get("offline_cache") is True for item in items
                    ),
                }
            )
            save_recall_observation(value, receipt)
            metadata_io.write(stamp, {"at": time.time()})
        if observe is not None:
            observe(receipt)
        return result
    # Recall waits for the actual question rather than injecting unrelated project
    # constraints from a generic startup query. No transcript or file scan.
    return {}


def flush(value):
    root = settings_path(value["device_home"]).parent / "outboxes"
    results = []
    if not root.exists():
        return {"status": "no-global-outbox"}
    for directory in root.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        local = metadata_io.private_json(directory / "origin.json", cache=True)
        if not approved(value) or select(value["scope"], local) is None:
            continue
        client, queue = adapter(value, local)
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
                    event_gate=lambda: approved(value)
                    and client.status(event["session_id"]).get("source_admitted")
                    is True,
                )
            )
    return {
        "status": "global-original-queue-sync",
        "results": results,
        "model_requests_reissued_directly": False,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "action",
        choices=(
            "global-install",
            "global-uninstall",
            "global-status",
            "global-hook",
            "global-configure",
            "global-recall-configure",
            "global-stop",
            "global-resume",
            "global-sync",
            "global-recall",
        ),
    )
    p.add_argument("--device-home", default=str(Path.home()))
    p.add_argument("--include-root", action="append", default=[])
    p.add_argument("--isolate-root", action="append", default=[])
    p.add_argument("--exclude-root", action="append", default=[])
    p.add_argument("--non-project-root", action="append", default=[])
    p.add_argument(
        "--approve-scope",
        help="Compatibility argument; configuration no longer requires a repeated digest approval",
    )
    p.add_argument("--all-projects", action="store_true")
    p.add_argument("--profile-id")
    p.add_argument("--packet-output")
    p.add_argument("--activate-packet")
    p.add_argument(
        "--check-server",
        action="store_true",
        help="Authenticated metadata-only source registration check; does not enable capture",
    )
    p.add_argument(
        "--request-key",
        help="Read a specific private recall receipt with global-status",
    )
    p.add_argument("--operator")
    p.add_argument("--operator-sha256")
    p.add_argument(
        "--recall-options",
        help="JSON native recall settings; maxResults is the per-namespace L1 count",
    )
    p.add_argument("--query", default="personal preferences and task progress")
    a = p.parse_args()
    observation = {}
    diagnostic = {"stage": "global-settings"}
    try:
        if a.action in ("global-install", "global-uninstall"):
            from agent_brain_client.adapters.harnesses.codex.collection_installation import (
                install,
                uninstall,
            )

            result = install(a) if a.action == "global-install" else uninstall(a)
            if a.action == "global-install":
                from agent_brain_client.runtime.installation_completion import (
                    source_setup,
                )

                result["source_setup"] = source_setup(a.device_home)
        elif a.action == "global-configure":
            from agent_brain_client.runtime.source_enrollment import configure

            result = configure(a)
        else:
            # Status reads only validated metadata. An official checkout may
            # legitimately differ from the installed frozen Hook code; do not
            # use that mismatch to suppress the authenticated status probe.
            value = load(a.device_home, verify_code=a.action != "global-status")
            if a.action == "global-recall-configure":
                from agent_brain_client.domain.contract import recall_options

                options = (
                    json.loads(a.recall_options) if a.recall_options is not None else {}
                )
                if not isinstance(options, dict):
                    raise ValueError("Native recall options required")
                settings = dict(options)
                limit = settings.pop("maxResults", 5)
                if type(limit) is not int or not 1 <= limit <= 2**31 - 1:
                    raise ValueError("Native L1 count required")
                recall_options(settings)
                value["recall"] = options
                metadata_io.write(settings_path(value["device_home"]), value)
                result = {
                    "status": "native-recall-configured",
                    "recall": options,
                    "source_scope_changed": False,
                    "model_calls": 0,
                }
            elif a.action == "global-hook":
                from agent_brain_client.application.control import RequestControl
                from agent_brain_client.adapters.storage.content import (
                    operation_diagnostics,
                )

                control = RequestControl(110)
                try:
                    with operation_diagnostics() as progress:
                        try:
                            with control.activate():
                                result = hook(value, observe=observation.update)
                        finally:
                            diagnostic.update(progress)
                finally:
                    control.close()
            elif a.action == "global-status":
                from agent_brain_client.adapters.harnesses.codex.collection_installation import (
                    installation_status,
                )

                installed = installation_status(value)
                result = {
                    "global_entry_installed": installed,
                    "expanded_sources_enabled": installed and approved(value),
                    "admission": admission_state(value),
                    "recall_observation": recall_observation(value, a.request_key),
                    "scope": value["scope"],
                    "legacy_project_roots": [
                        r["root"] for r in value["legacy_projects"]
                    ],
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
                if a.check_server:
                    result["server_admission"] = CollectionClient(value).wire(
                        "memory_global_status", {}
                    )
                    result["credentials_accessed"] = True
            elif a.action == "global-resume":
                result = resume(value)
            elif a.action == "global-stop":
                directory = settings_path(a.device_home).parent
                off = directory / "global.off"
                off.touch(mode=0o600)
                if value["server_scope_sha256"]:
                    client = CollectionClient(value)
                    result = client.wire("memory_global_stop", {})
                    if "error" in result:
                        raise ValueError("Remote global control unconfirmed")
                else:
                    for row in value["legacy_projects"]:
                        source = project_sources.load(
                            row["settings"], row["settings_sha256"]
                        )
                        marker = Path(source["off_file"])
                        marker.touch(mode=0o600)
                        result = project_sources.daily_command(source, "stop")
                result = {
                    "status": "global-memory-stopped",
                    "remote_confirmed": bool(
                        value["server_scope_sha256"] or value["legacy_projects"]
                    ),
                }
            elif a.action == "global-sync":
                result = flush(value)
            else:
                local = eligible(value, os.getcwd())
                if local is None:
                    raise ValueError("Current origin not approved")
                result = {
                    "items": recall(
                        value, local, "global-reader-" + os.urandom(12).hex(), a.query
                    ),
                    "native_hook_verified": False,
                }
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if a.action == "global-hook" and observation:
            observation["output_written"] = True
            try:
                save_recall_observation(value, observation)
            except (OSError, ValueError):
                pass  # stdout is already flushed; never emit a second JSON result.
            print(
                json.dumps(
                    dict(
                        observation,
                        status="hook-output-written",
                        recall_status=observation["status"],
                    )
                ),
                file=sys.stderr,
            )
        failed_activation = (
            a.action == "global-configure" and result.get("activated") is not True
        )
        return (
            2
            if failed_activation
            or result.get("status")
            in ("global-memory-activation-required", "global-memory-resume-unconfirmed")
            else 0
        )
    except Exception as error:
        if a.action == "global-hook":
            print("{}")
            print(
                json.dumps(
                    {
                        "stage": "agentbrain-global-memory",
                        "status": "review-required",
                        "operation_stage": diagnostic["stage"],
                        "error_type": type(error).__name__,
                        "preserve_existing_state": True,
                    }
                ),
                file=sys.stderr,
            )
            return 0
        print(
            json.dumps(
                {
                    "status": "global-memory-review-required",
                    "operation": a.action,
                    "credentials_returned": False,
                    "preserve_existing_state": True,
                }
            )
        )
        return 2


# Concrete collaborators are assembled here, outside the retrieval use case.
from functools import partial
from types import SimpleNamespace
from agent_brain_client.application.memory.recall import recall as recall_memory

recall = partial(
    recall_memory,
    services=SimpleNamespace(
        client=lambda value, local: CollectionClient(value, local),
        settings_path=lambda home: settings_path(home),
        read_cache=lambda *args, **kwargs: metadata_io.private_json(*args, **kwargs),
        write_cache=lambda *args, **kwargs: metadata_io.write(*args, **kwargs),
        approved=lambda value: approved(value),
    ),
)

if __name__ == "__main__":
    raise SystemExit(main())
