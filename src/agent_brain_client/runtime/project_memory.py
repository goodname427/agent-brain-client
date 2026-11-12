"""Compose project-scoped capture, recall and approved source operations."""

import json
import os
from pathlib import Path
import sys
import time
import tempfile
from agent_brain_client.adapters.platforms.filesystem import command_line
from agent_brain_client.domain.privacy import private_content
from agent_brain_client.adapters.storage.registration import registration
from agent_brain_client.adapters.storage.metadata import (
    canonical,
    digest,
    private_json,
    unique,
    write,
)
from agent_brain_client.adapters.platforms.filesystem import (
    private_mode,
    private_directory,
    locked,
)
from agent_brain_client.adapters.packages.layout import PYTHON_FILES as RUNTIME
from agent_brain_client.adapters.harnesses.codex.project_configuration import (
    load,
    active,
    project_settings,
    install,
    context,
)

ROOT = Path(__file__).resolve().parents[3]
ENTRY = ROOT / "src/agent_brain_client/interfaces/cli/project.py"


def setup(args):
    home = canonical(args.device_home, existing=True)
    project = canonical(args.project_root, existing=True)
    m, policy = registration(home)
    r = policy["runtime"]
    output = canonical(args.output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.exists():
        raise ValueError(
            "Existing setup preserved; use a new output or explicit refresh"
        )
    value = {
        "schema": 1,
        "enabled": True,
        "project_root": str(project),
        "device_home": str(home),
        "codex_home": r["codex_home"],
        "profile": r["https_profile"],
        "profile_sha256": r["https_profile_sha256"],
        "content_policy": r["content_policy"],
        "content_policy_sha256": r["content_policy_sha256"],
        "release_policy": m["release_policy"],
        "release_policy_sha256": m["release_policy_sha256"],
        "cache": r["content_cache"],
        "off_file": str(output.parent / "memory.off"),
        "query": args.query,
        "max_items": 3,
        "max_context_bytes": 12288,
        "code_sha256": {n: digest((ROOT / n).read_bytes()) for n in RUNTIME},
        "capture_policy": None,
        "capture_policy_sha256": None,
        "recall_policy": None,
        "recall_policy_sha256": None,
        "source_mode": "shared-memory",
    }
    if args.server_bundle:
        bundle = private_json(args.server_bundle, args.server_bundle_sha256)
        if (
            set(bundle)
            != {
                "schema",
                "source_mode",
                "client_capture_policy",
                "client_capture_policy_sha256",
                "client_recall_policy",
                "client_recall_policy_sha256",
            }
            or bundle["schema"] != 1
        ):
            raise ValueError("Reviewed Server bundle receipt required")
        value.update(
            source_mode=bundle["source_mode"],
            capture_policy=bundle["client_capture_policy"],
            capture_policy_sha256=bundle["client_capture_policy_sha256"],
            recall_policy=bundle["client_recall_policy"],
            recall_policy_sha256=bundle["client_recall_policy_sha256"],
        )
    pin = write(output, value, exclusive=True)
    load(output, pin)
    return {
        "status": "memory-settings-prepared",
        "settings": str(output),
        "settings_sha256": pin,
        "source_mode": value["source_mode"],
        "capture_enabled": False,
        "hooks_installed": False,
        "next_command": command_line(
            [
                sys.executable,
                str(ENTRY),
                "install",
                "--settings",
                str(output),
                "--settings-sha256",
                pin,
            ]
        ),
    }


def enable(args):
    if not args.project_root:
        raise ValueError("Exact --project-root required")
    output = (
        canonical(args.output)
        if args.output
        else project_settings(args.device_home, args.project_root)
    )
    args.output = str(output)
    if output.exists():
        before = output.read_bytes()
        value = private_json(output, digest(before))
        if (
            value["project_root"] != str(canonical(args.project_root, existing=True))
            or value["device_home"] != str(canonical(args.device_home, existing=True))
            or args.server_bundle
        ):
            raise ValueError(
                "Existing project scope preserved; use explicit setup for a new authorized bundle"
            )
        # Refresh only source pins, never destination, session scope or expiry.
        value = dict(
            value,
            enabled=True,
            code_sha256={n: digest((ROOT / n).read_bytes()) for n in RUNTIME},
        )
        fd, candidate = tempfile.mkstemp(prefix=".settings-check-", dir=output.parent)
        os.close(fd)
        try:
            pin = write(candidate, value)
            load(candidate, pin)
            if output.read_bytes() != before:
                raise ValueError("Settings changed during refresh")
            pin = write(output, value)
        finally:
            os.unlink(candidate)
    else:
        result = setup(args)
        pin = result["settings_sha256"]
    p = load(output, pin)
    marker = canonical(p["off_file"])
    if marker.exists():
        if not marker.is_file() or (
            os.name != "nt"
            and (marker.stat().st_uid != os.getuid() or marker.stat().st_mode & 0o077)
        ):
            raise ValueError("Unknown stop marker preserved")
        private_mode(marker, owner=True)
        marker.unlink()
    if not active(p):
        raise ValueError("Source window closed; enabling does not renew it")
    result = install(p, str(output), pin)
    return dict(
        result,
        settings=str(output),
        settings_sha256=pin,
        enabled=True,
        source_mode=p["source_mode"],
        model_probe_sent=False,
    )


def import_bundle(args):
    manifest = private_json(args.activation, args.activation_sha256)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") not in (1, 2)
        or manifest.get("runtime_activated") is not False
        or manifest.get("model_calls_sent") != 0
    ):
        raise ValueError("Reviewed compiler activation receipt required")
    directory = canonical(Path(args.activation).parent, existing=True)
    target = canonical(manifest["client_policy_directory"])
    private_directory(target)
    private_mode(target, directory=True, owner=True)
    bundle = private_json(
        directory / "client-bundle.json", manifest["files"]["client-bundle.json"]
    )
    capture = private_json(
        directory / "client-capture.json", manifest["files"]["client-capture.json"]
    )
    recall = private_json(
        directory / "client-recall.json", manifest["files"]["client-recall.json"]
    )
    _, registered = registration(canonical(args.device_home, existing=True))
    r = registered["runtime"]
    native_home = canonical(r["codex_home"])
    if target == native_home or native_home in target.parents:
        raise ValueError(
            "Native history/config directories are never policy or note stores"
        )
    if capture.get("schema") == 2:
        approved_root = canonical(
            Path(args.device_home) / ".config/agent-brain-client/project-memory"
        )
        if approved_root not in target.parents:
            raise ValueError(
                "Persistent source metadata belongs in the private Client project-memory directory"
            )
    if capture.get("schema") == 2 and capture.get("source_mode") == "operator-notes":
        notes = canonical(capture["notes_directory"])
        if notes != target / "notes":
            raise ValueError("Dedicated private note directory required")
        private_directory(notes)
        private_mode(notes, directory=True, owner=True)
    profile = private_json(r["https_profile"], r["https_profile_sha256"])
    if (
        capture["profile_sha256"] != r["https_profile_sha256"]
        or capture["content_policy_sha256"] != r["content_policy_sha256"]
        or recall["profile_sha256"] != r["https_profile_sha256"]
        or recall["endpoint"] != profile["endpoint"]
        or bundle["client_capture_policy"] != str(target / "client-capture.json")
        or bundle["client_recall_policy"] != str(target / "client-recall.json")
        or bundle["client_capture_policy_sha256"]
        != manifest["files"]["client-capture.json"]
        or bundle["client_recall_policy_sha256"]
        != manifest["files"]["client-recall.json"]
    ):
        raise ValueError("Compiled Client identity/destination differs")
    for name in ("client-capture.json", "client-recall.json", "client-bundle.json"):
        source = directory / name
        pin = manifest["files"][name]
        value = private_json(source, pin)
        destination = canonical(target / name)
        if destination.exists():
            private_json(destination, pin)
        elif write(destination, value, exclusive=True) != pin:
            raise ValueError("Compiler serialization changed")
    return {
        "status": "client-policies-imported",
        "bundle": str(target / "client-bundle.json"),
        "bundle_sha256": manifest["files"]["client-bundle.json"],
        "hooks_installed": False,
        "model_calls_sent": 0,
    }


def recalled(p, session, query=None):
    p = dict(p, query=query[:1024]) if query is not None else p
    from agent_brain_client.adapters.server.https import HTTPSContentClient

    profile = private_json(p["profile"], p["profile_sha256"])
    if p["recall_policy"]:
        policy = private_json(p["recall_policy"], p["recall_policy_sha256"])
        daily = policy.get("schema") == 2
        if daily:
            session = (
                session
                if policy["source_mode"] == "codex-project" and session
                else policy["recall_session_id"]
            )
        elif session not in policy["recall_sessions"]:
            return []
        client = HTTPSContentClient(
            p["profile"],
            p["profile_sha256"],
            recall_policy=(p["recall_policy"], p["recall_policy_sha256"]),
        )
        query = p["query"].strip() or "personal preferences and task progress"
        params = {
            "kind": "memory",
            "query": query,
            "since": None,
            "until": None,
            "limit": p["max_items"],
            "context": {
                "project_id": policy["project_id"],
                "session_id": session,
                "cross_session": True,
            },
            **(
                {"source_scope": "approved-project"}
                if daily
                else {"source_sessions": policy["source_sessions"]}
            ),
        }
        result = client.request(
            {
                "request_id": "recall_" + os.urandom(16).hex(),
                "method": "memory_query",
                "params": params,
            }
        )
        if daily:
            cache_path = canonical(
                Path(p["off_file"]).parent
                / (
                    "derived-"
                    + digest((p["profile_sha256"] + p["recall_policy_sha256"]).encode())
                    + ".json"
                )
            )
            binding = {
                "profile_sha256": p["profile_sha256"],
                "policy_sha256": p["recall_policy_sha256"],
                "project_id": policy["project_id"],
                "device_id": client.device_id,
            }
            if "error" in result:
                from agent_brain_client.adapters.harnesses.codex.content_limits import (
                    OFFLINE_TRANSPORT_ERRORS,
                )

                if result["error"].get("code") not in OFFLINE_TRANSPORT_ERRORS:
                    raise ValueError("Derived recall denied or unavailable")
                saved = private_json(cache_path)
                if saved.get("binding") != binding or saved.get(
                    "payload_sha256"
                ) != digest(
                    json.dumps(
                        saved.get("payload"), sort_keys=True, ensure_ascii=False
                    ).encode()
                ):
                    raise ValueError("Derived snapshot changed")
                from agent_brain_client.domain.contract import core_recall

                body = saved["payload"]
                core_recall(body, body["scope"])
                if (
                    body["scope"]["project_id"] != policy["project_id"]
                    or body["recall"]["L0"]
                ):
                    raise ValueError("Cached project scope differs")
                return [
                    {
                        "layer": k,
                        "content": row["content"],
                        "cached_snapshot": True,
                        "remote_confirmed": False,
                    }
                    for k in ("L1", "L2", "L3")
                    for row in (body["recall"][k] if k != "L3" else [body["recall"][k]])
                    if row.get("content")
                ]
            write(
                cache_path,
                {
                    "schema": 1,
                    "binding": binding,
                    "payload": result["result"],
                    "payload_sha256": digest(
                        json.dumps(
                            result["result"], sort_keys=True, ensure_ascii=False
                        ).encode()
                    ),
                    "verified_at": int(time.time()),
                },
            )
        elif "error" in result:
            raise ValueError("Derived memory recall unavailable; no model invoked")
        layers = result["result"]["recall"]
        selected = [
            {"layer": key, "content": row["content"]}
            for key in ("L1", "L2", "L3")
            for row in (layers[key] if key != "L3" else [layers[key]])
            if row.get("content")
        ]
        if selected:
            return selected
        # Already authorized canonical Memory remains usable without importing it
        # into Core or relabelling it as extracted model output.
        return recalled(
            dict(
                p,
                recall_policy=None,
                recall_policy_sha256=None,
                source_mode="shared-memory",
            ),
            session,
        )
    if "memory" not in profile["grant"]["artifact_types"]:
        raise ValueError("Existing Memory read grant required")
    client = HTTPSContentClient(p["profile"], p["profile_sha256"])
    from agent_brain_client.adapters.harnesses.codex.content import CodexContentSession
    from agent_brain_client.domain.content_policy import validate_policy

    content = private_json(p["content_policy"], p["content_policy_sha256"])
    validate_policy(content, client.grant)
    adapter = CodexContentSession(
        p["device_home"], p["cache"], content, client, codex_home=p["codex_home"]
    )
    query = adapter.invoke(
        {
            "request_id": "recall_" + os.urandom(16).hex(),
            "method": "memory_query",
            "params": {
                "kind": "memory",
                "query": p["query"],
                "since": None,
                "until": None,
                "limit": p["max_items"],
            },
        }
    )
    offline = "error" in query
    if offline:
        from agent_brain_client.adapters.harnesses.codex.content_limits import (
            OFFLINE_TRANSPORT_ERRORS,
        )

        if query["error"].get("code") not in OFFLINE_TRANSPORT_ERRORS:
            raise ValueError("Shared memory query unavailable")

        def cached_items():
            cursor = None
            seen = set()
            while True:
                cached = adapter._offline_read(
                    {
                        "request_id": "cached_" + os.urandom(16).hex(),
                        "method": "content_catalog",
                        "params": dict(
                            kinds=["memory"], **({"cursor": cursor} if cursor else {})
                        ),
                    }
                )
                if "error" in cached:
                    raise ValueError("No approved cached Memory snapshot")
                page = cached["result"]
                if not isinstance(page.get("artifacts"), list):
                    raise ValueError("Unknown cached catalog response")
                yield from page["artifacts"]
                cursor = page.get("next_cursor")
                if cursor is None:
                    return
                if cursor in seen:
                    raise ValueError("Cached catalog did not advance")
                seen.add(cursor)

        items = cached_items()
    else:
        items = query["result"].get("topics")
    if not offline and not isinstance(items, list):
        raise ValueError("Unknown memory query response")
    rows = []
    for item in items if offline else items[: p["max_items"]]:
        request = {
            "request_id": "recall_" + os.urandom(16).hex(),
            "method": "content_read",
            "params": {"kind": "memory", "id": item["id"]},
        }
        result = adapter._offline_read(request) if offline else adapter.invoke(request)
        if "error" in result:
            raise ValueError("Memory read unavailable")
        record = result["result"]["record"]
        if record is not None:
            if (
                offline
                and p["query"].strip()
                and not any(
                    t in (record["title"] + " " + record["body"]).casefold()
                    for t in p["query"].casefold().split()
                )
            ):
                continue
            rows.append(
                {
                    "id": item["id"],
                    "title": record["title"],
                    "content": record["body"],
                    "cached_snapshot": offline,
                }
            )
            if len(rows) == p["max_items"]:
                break
    return rows


def capture(p, payload):
    from agent_brain_client.adapters.storage.pinned import pinned
    from agent_brain_client.adapters.harnesses.codex.hooks import (
        validate_hook_policy,
        native_hook_fields,
        hook_enabled,
    )
    from agent_brain_client.adapters.harnesses.codex.content import CodexContentSession
    from agent_brain_client.domain.content_policy import validate_policy
    from agent_brain_client.adapters.server.https import HTTPSContentClient

    policy = validate_hook_policy(
        pinned(p["capture_policy"], p["capture_policy_sha256"]),
        profile_sha256=p["profile_sha256"],
        content_policy_sha256=p["content_policy_sha256"],
    )
    if not hook_enabled(policy):
        return {"status": "disabled", "data_transferred": False}
    scope, turn, kind, text, key = native_hook_fields(policy, payload)
    if policy.get("schema") == 2:
        blocked = canonical(Path(p["off_file"]).parent / ("excluded-" + key + ".json"))
        if blocked.exists():
            return {"status": "excluded-turn", "data_transferred": False}
        if private_content(text):
            write(
                blocked,
                {"schema": 1, "reason": "local-only-or-sensitive", "turn_sha256": key},
                exclusive=True,
            )
            return {"status": "excluded-turn", "data_transferred": False}
    client = HTTPSContentClient(p["profile"], p["profile_sha256"])
    content = pinned(p["content_policy"], p["content_policy_sha256"])
    validate_policy(content, client.grant)
    adapter = CodexContentSession(
        p["device_home"], p["cache"], content, client, codex_home=p["codex_home"]
    )
    if policy.get("schema") == 2 and kind == "Stop":
        staged = adapter._load("hook-turns/" + key + ".json")
        if isinstance(staged, dict) and private_content(staged.get("prompt", "")):
            blocked = canonical(
                Path(p["off_file"]).parent / ("excluded-" + key + ".json")
            )
            if not blocked.exists():
                write(
                    blocked,
                    {
                        "schema": 1,
                        "reason": "local-only-or-sensitive",
                        "turn_sha256": key,
                    },
                    exclusive=True,
                )
            return {"status": "excluded-turn", "data_transferred": False}
    from agent_brain_client.application.memory.observations import stage_messages
    previous = adapter._load("outbox/agentbrain_event_" + key + ".json")
    if previous is not None:
        return dict(adapter._queue_result(previous), status="retained-paired-event")
    return stage_messages(adapter, project_id=scope["project_id"], session_id=scope["session_id"],
                          operation_id=turn, phase="prompt" if kind == "UserPromptSubmit" else "assistant",
                          messages=[{"role": "user" if kind == "UserPromptSubmit" else "assistant", "content": text}])


def record(p, path):
    """Explicit user-supplied note pair; never label it a native hook event."""
    if p["source_mode"] != "operator-notes" or not active(p):
        raise ValueError("Explicit operator-note scope required")
    if daily(p):
        return record_daily(p, path)
    note = private_json(path)
    if not isinstance(note, dict) or set(note) != {
        "session_id",
        "turn_id",
        "user",
        "assistant",
    }:
        raise ValueError("Explicit note pair required")
    pass
    from agent_brain_client.domain.contract import event_identifier, text
    from agent_brain_client.adapters.harnesses.codex.content import CodexContentSession
    from agent_brain_client.domain.content_policy import validate_policy
    from agent_brain_client.adapters.server.https import HTTPSContentClient

    for name in ("session_id", "turn_id"):
        event_identifier(note[name])
    for name in ("user", "assistant"):
        text(note[name], 16384)
    policy = private_json(p["capture_policy"], p["capture_policy_sha256"])
    scope = next(
        (
            s
            for s in policy["scopes"]
            if s["cwd"] == p["project_root"] and s["session_id"] == note["session_id"]
        ),
        None,
    )
    if scope is None:
        raise ValueError("Note source not authorized")
    client = HTTPSContentClient(p["profile"], p["profile_sha256"])
    content = private_json(p["content_policy"], p["content_policy_sha256"])
    validate_policy(content, client.grant)
    adapter = CodexContentSession(
        p["device_home"], p["cache"], content, client, codex_home=p["codex_home"]
    )
    from datetime import datetime, timezone

    key = digest(
        json.dumps(
            [scope, note["turn_id"]], sort_keys=True, ensure_ascii=False
        ).encode()
    )
    fingerprint = digest(json.dumps(note, sort_keys=True, ensure_ascii=False).encode())
    with locked(adapter.cache / "operator-notes.lock"):
        if not active(p):
            raise ValueError("Source window closed")
        name = "operator-notes/" + key + ".json"
        saved = adapter._load(name)
        if saved is None:
            directory = adapter.cache / "operator-notes"
            if directory.exists() and len(list(directory.iterdir())) >= 1024:
                raise ValueError("Operator note staging is full")
            event = {
                "contract": "agentbrain.event.v1",
                "event_id": "note-" + key[:56],
                "project_id": scope["project_id"],
                "session_id": scope["session_id"],
                "occurred_at": datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "messages": [
                    {"role": "user", "content": note["user"]},
                    {"role": "assistant", "content": note["assistant"]},
                ],
            }
            saved = {
                "origin": "operator-provided-note-pair",
                "input_sha256": fingerprint,
                "event": event,
            }
            adapter._save(name, saved)
        if saved.get("input_sha256") != fingerprint:
            raise ValueError(
                "Original note changed; preserve the same operation for review"
            )
        event = saved["event"]
        response = adapter.invoke(
            {
                "request_id": "agentbrain_note_" + key,
                "method": "memory_event_submit",
                "params": {"kind": "memory", "id": event["event_id"], "event": event},
            }
        )
    return {
        "origin": "operator-provided-note-pair",
        "native_hook_verified": False,
        "event_id": event["event_id"],
        "result": response,
        "model_success_claimed": False,
    }


def hook_payload(p, *, settings_path=None, settings_sha256=None):
    if not active(p):
        return {}
    started = time.monotonic()
    raw = sys.stdin.buffer.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("Bounded native payload required")
    payload = json.loads(raw, object_pairs_hook=unique)
    if payload.get("cwd") != p["project_root"]:
        return {}
    event = payload.get("hook_event_name")
    from agent_brain_client.application.memory.lifecycle import handle
    from agent_brain_client.runtime.delivery import schedule_project

    def stage():
        if p["source_mode"] not in ("codex-hooks", "codex-project"):
            return {"status": "recall-only", "data_transferred": False}
        return capture(p, payload)

    def schedule():
        if p["capture_policy"] is not None:
            if settings_path is None or settings_sha256 is None:
                raise ValueError("Pinned project settings required for delivery")
            schedule_project(p["device_home"], settings_path, settings_sha256)

    items, diagnostic = handle(event, prompt=payload.get("prompt", ""), stage=stage,
        schedule=schedule, recall=lambda query: recalled(p, payload.get("session_id"), query))
    diagnostic["timing_ms"]["adapter_total_ms"] = round((time.monotonic() - started) * 1000, 3)
    print(json.dumps(dict(operation="native-lifecycle", harness="codex-project", **diagnostic)), file=sys.stderr)
    extra = context(p, items)
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": extra}} if extra else {}


def hook(p, *, settings_path=None, settings_sha256=None):
    if not active(p):
        return {}
    from agent_brain_client.application.control import RequestControl
    control = RequestControl(25)
    try:
        with control.activate():
            return hook_payload(p, settings_path=settings_path, settings_sha256=settings_sha256)
    finally:
        control.close()


def delivery_port(settings, sha):
    from agent_brain_client.adapters.harnesses.codex.hooks import validate_hook_policy, hook_enabled
    p = load(settings, sha)
    if not active(p) or p["capture_policy"] is None:
        raise ValueError("Project source inactive")
    client, queue = adapter_for(p)
    policy = validate_hook_policy(private_json(p["capture_policy"], p["capture_policy_sha256"]),
        profile_sha256=p["profile_sha256"], content_policy_sha256=p["content_policy_sha256"])
    from agent_brain_client.adapters.storage.observed_migration import migrate_staged_turns
    migrate_staged_turns(queue, origin={"cwd": p["project_root"]},
        project_ids={scope["project_id"] for scope in policy["scopes"] if scope["cwd"] == p["project_root"]},
        excluded_directory=Path(p["off_file"]).parent)

    def allowed(session):
        current = load(settings, sha)
        if not active(current):
            return False
        policy = validate_hook_policy(private_json(current["capture_policy"], current["capture_policy_sha256"]),
            profile_sha256=current["profile_sha256"], content_policy_sha256=current["content_policy_sha256"])
        if not hook_enabled(policy):
            return False
        if not any(scope["cwd"] == current["project_root"] and scope["session_id"] in (None, session)
                   for scope in policy["scopes"]):
            return False
        return daily_gate(current, client)() if policy["schema"] == 2 else True
    def allowed_event(event):
        current = load(settings, sha)
        policy = validate_hook_policy(private_json(current["capture_policy"], current["capture_policy_sha256"]),
            profile_sha256=current["profile_sha256"], content_policy_sha256=current["content_policy_sha256"])
        return active(current) and hook_enabled(policy) and any(
            scope["cwd"] == current["project_root"]
            and scope["project_id"] == event["project_id"]
            and scope["session_id"] in (None, event["session_id"])
            for scope in policy["scopes"])
    return queue, allowed, allowed_event


def daily(p):
    return (
        bool(p["recall_policy"])
        and private_json(p["recall_policy"], p["recall_policy_sha256"]).get("schema")
        == 2
    )


def daily_status(
    p, client, *, resume=False, action=None, event_id=None, session_id=None
):
    diagnostic = event_id is not None or session_id is not None
    if diagnostic:
        from agent_brain_client.domain.events import event_identifier

        event_identifier(event_id)
        event_identifier(session_id)
        if resume or action is not None:
            raise ValueError("Event diagnostics require read-only status")
    policy = private_json(p["recall_policy"], p["recall_policy_sha256"])
    request = {
        "request_id": "daily_status_" + os.urandom(16).hex(),
        "method": ("memory_" + action)
        if action
        else ("memory_resume" if resume else "memory_status"),
        "params": {
            "project_id": policy["project_id"],
            "session_id": policy["recall_session_id"],
        },
    }
    if diagnostic:
        request["params"].update(event_id=event_id, session_id=session_id)
    result = client.request(request)
    if "error" in result:
        raise ValueError("Persistent memory admission unavailable")
    value = result["result"]
    if diagnostic:
        if not isinstance(value, dict) or not isinstance(
            value.get("capture_extraction"), dict
        ):
            from agent_brain_client.domain.errors import EventDiagnosticsUnavailable

            raise EventDiagnosticsUnavailable(
                "Event diagnostics unavailable on this Server"
            )
        return value
    if (
        not isinstance(value, dict)
        or value.get("schema") != 2
        or value.get("lifecycle") != "until-disabled"
        or type(value.get("model_admitted")) is not bool
        or type(value.get("source_admitted")) is not bool
    ):
        raise ValueError("Invalid persistent admission status")
    return value


def daily_gate(p, client):
    checked: list[dict] = []

    def gate():
        if not active(p):
            return False
        if not checked:
            checked.append(daily_status(p, client))
        return checked[0]["source_admitted"] is True

    return gate


def adapter_for(p):
    from agent_brain_client.adapters.server.https import HTTPSContentClient
    from agent_brain_client.adapters.harnesses.codex.content import CodexContentSession
    from agent_brain_client.domain.content_policy import validate_policy

    client = HTTPSContentClient(p["profile"], p["profile_sha256"])
    content = private_json(p["content_policy"], p["content_policy_sha256"])
    validate_policy(content, client.grant)
    return client, CodexContentSession(
        p["device_home"], p["cache"], content, client, codex_home=p["codex_home"]
    )


def record_daily(p, path):
    if not active(p):
        raise ValueError("Persistent source is stopped")
    source = canonical(path, existing=True)
    if (
        not source.is_file()
        or (
            os.name != "nt"
            and (source.stat().st_mode & 0o077 or source.stat().st_uid != os.getuid())
        )
        or source.stat().st_size > 32768
    ):
        raise ValueError("Explicit private note file required")
    private_mode(source, owner=True)
    capture_policy = private_json(p["capture_policy"], p["capture_policy_sha256"])
    notes = canonical(capture_policy["notes_directory"], existing=True)
    if notes not in source.parents or source.suffix.lower() not in (
        ".txt",
        ".md",
        ".json",
    ):
        raise ValueError("Only the configured private note directory is a source")
    text = source.read_text(encoding="utf-8")
    if private_content(text):
        return {
            "status": "excluded-local-only-or-sensitive",
            "data_transferred": False,
            "model_calls_sent": 0,
        }
    policy = private_json(p["recall_policy"], p["recall_policy_sha256"])
    pass
    from agent_brain_client.domain.contract import event_identifier, text as valid_text

    try:
        note = json.loads(text, object_pairs_hook=unique)
    except ValueError:
        note = {"text": text}
    if not isinstance(note, dict) or set(note) not in (
        {"text"},
        {"id", "text"},
        {"user", "assistant"},
        {"id", "user", "assistant"},
    ):
        raise ValueError("Plain note or explicit text/pair JSON required")
    fields = ("text",) if "text" in note else ("user", "assistant")
    for n in fields:
        valid_text(note[n], 32768)
        if len(note[n].encode("utf-16-le")) // 2 > 8192:
            raise ValueError("Note exceeds supported message size")
    if any(private_content(note[n]) for n in fields):
        return {
            "status": "excluded-local-only-or-sensitive",
            "data_transferred": False,
            "model_calls_sent": 0,
        }
    identity = note.get("id") or digest(
        json.dumps(note, sort_keys=True, ensure_ascii=False).encode()
    )
    event_identifier(identity)
    scope = {
        "project_id": policy["project_id"],
        "session_id": policy["source_session_id"],
    }
    fingerprint = digest(json.dumps(note, sort_keys=True, ensure_ascii=False).encode())
    client, adapter = adapter_for(p)
    from datetime import datetime, timezone

    key = digest(json.dumps([scope, identity], sort_keys=True).encode())
    with locked(adapter.cache / "operator-notes.lock"):
        saved = adapter._load("operator-notes/" + key + ".json")
        if saved is None:
            event = {
                "contract": "agentbrain.event.v1",
                "event_id": "note-" + key[:56],
                **scope,
                "occurred_at": datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "messages": [
                    {
                        "role": "user" if n in ("user", "text") else "assistant",
                        "content": note[n],
                    }
                    for n in fields
                ],
            }
            saved = {
                "origin": "operator-provided-note",
                "input_sha256": fingerprint,
                "event": event,
            }
            adapter._save("operator-notes/" + key + ".json", saved)
        if saved.get("input_sha256") != fingerprint:
            raise ValueError("Same note ID changed; original operation preserved")
        event = saved["event"]
        request = {
            "request_id": "agentbrain_note_" + key,
            "method": "memory_event_submit",
            "params": {"kind": "memory", "id": event["event_id"], "event": event},
        }
        response = adapter.invoke(request, event_gate=daily_gate(p, client))
    return {
        "origin": "operator-provided-note",
        "native_hook_verified": False,
        "event_id": event["event_id"],
        "result": response,
        "next": "recall reads derived memory; resume handles known admission pauses without replaying uncertain model requests",
    }


def daily_command(p, action, *, event_id=None, session_id=None):
    if event_id is not None or session_id is not None:
        from agent_brain_client.domain.events import event_identifier

        event_identifier(event_id)
        event_identifier(session_id)
        if action != "server-status":
            raise ValueError("Event diagnostics require server-status")
    if not daily(p):
        raise ValueError("Persistent source configuration required")
    if action == "flush" and not active(p):
        raise ValueError("Source is stopped; explicitly enable before resuming")
    if event_id is not None:
        from agent_brain_client.adapters.server.https import HTTPSContentClient

        client = HTTPSContentClient(p["profile"], p["profile_sha256"])
        return daily_status(p, client, event_id=event_id, session_id=session_id)
    client, adapter = adapter_for(p)
    if action in ("stop", "revoke"):
        return daily_status(p, client, action=action)
    if action == "server-status":
        return daily_status(p, client, event_id=event_id, session_id=session_id)
    if action == "sync":
        return daily_status(p, client, action="sync")
    if action == "resume":
        result = daily_status(p, client, resume=p["source_mode"] != "recall-only")
        marker = canonical(p["off_file"])
        if marker.exists():
            if not marker.is_file() or (
                os.name != "nt"
                and (
                    marker.stat().st_uid != os.getuid() or marker.stat().st_mode & 0o077
                )
            ):
                raise ValueError("Unknown local source marker preserved")
            private_mode(marker, owner=True)
            marker.unlink()
        return result
    policy = private_json(p["recall_policy"], p["recall_policy_sha256"])
    gate = daily_gate(p, client)
    results = []
    directory = adapter.cache / "outbox"
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            row = adapter._load("outbox/" + path.name)
            if not isinstance(row, dict) or row.get("state") == "confirmed":
                continue
            request = row.get("request", {})
            event = request.get("params", {}).get("event", {})
            if (
                request.get("method") != "memory_event_submit"
                or event.get("project_id") != policy["project_id"]
            ):
                continue
            if (
                policy["source_mode"] == "operator-notes"
                and event.get("session_id") != policy["source_session_id"]
            ):
                continue
            if any(
                private_content(m.get("content", "")) for m in event.get("messages", [])
            ):
                continue
            if not gate():
                break
            results.append(
                {
                    "request_id": request["request_id"],
                    "result": adapter.invoke(request, event_gate=gate),
                }
            )
            if len(results) >= 16:
                break
    return {
        "replayed_original_operations": results,
        "model_requests_reissued_directly": False,
    }


def activate_bundle(args):
    if not args.activation or not args.activation_sha256 or not args.project_root:
        raise ValueError("Reviewed activation receipt and project required")
    imported = import_bundle(args)
    output = project_settings(args.device_home, args.project_root)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    old = output.read_bytes() if output.exists() else None
    if old is not None:
        previous = private_json(output, digest(old))
        if previous["project_root"] != str(canonical(args.project_root, existing=True)):
            raise ValueError("Existing project differs")
        backup = output.with_name("settings.previous." + digest(old) + ".json")
        if not backup.exists():
            write(backup, previous, exclusive=True)
    args.server_bundle = imported["bundle"]
    args.server_bundle_sha256 = imported["bundle_sha256"]
    args.output = str(
        output.with_name("settings.next." + args.activation_sha256 + ".json")
    )
    candidate = Path(args.output)
    if candidate.exists():
        pin = digest(candidate.read_bytes())
        value = load(candidate, pin)
    else:
        prepared = setup(args)
        pin = prepared["settings_sha256"]
        value = load(candidate, pin)
    if (output.read_bytes() if output.exists() else None) != old:
        raise ValueError("Project settings changed during activation")
    pin = write(output, value)
    args.output = str(output)
    args.server_bundle = None
    return enable(args)


def project_link(p, output):
    if not daily(p):
        raise ValueError("Persistent project required")
    r = private_json(p["recall_policy"], p["recall_policy_sha256"])
    value = {
        k: r[k]
        for k in (
            "actor",
            "endpoint",
            "project_id",
            "source_mode",
            "source_device_id",
            "source_session_id",
            "recall_session_id",
        )
    }
    value.update(schema=1, scope="existing-owner-devices-read-only")
    pin = write(output, value, exclusive=True)
    return {
        "project_link": str(output),
        "sha256": pin,
        "contains_memory_or_credentials": False,
        "source_permissions_added": False,
    }


def connect_project(args):
    if not args.input or not args.project_root:
        raise ValueError("Explicit project link and local project required")
    link = private_json(args.input)
    fields = {
        "schema",
        "scope",
        "actor",
        "endpoint",
        "project_id",
        "source_mode",
        "source_device_id",
        "source_session_id",
        "recall_session_id",
    }
    if (
        set(link) != fields
        or link["schema"] != 1
        or link["scope"] != "existing-owner-devices-read-only"
    ):
        raise ValueError("Read-only project link required")
    _, registered = registration(canonical(args.device_home, existing=True))
    r = registered["runtime"]
    profile = private_json(r["https_profile"], r["https_profile_sha256"])
    if (
        link["endpoint"] != profile["endpoint"]
        or link["actor"] != profile["grant"]["actor"]
    ):
        raise ValueError("Project link differs from existing owner/server")
    pass
    from agent_brain_client.adapters.server.https import HTTPSContentClient

    client = HTTPSContentClient(r["https_profile"], r["https_profile_sha256"])
    response = client.request(
        {
            "request_id": "link_" + os.urandom(16).hex(),
            "method": "memory_status",
            "params": {
                "project_id": link["project_id"],
                "session_id": link["recall_session_id"],
            },
        }
    )
    if "error" in response:
        raise ValueError("Existing owner project grant unavailable")
    observed = response["result"]
    if any(
        observed.get(k) != link[k]
        for k in (
            "project_id",
            "source_mode",
            "source_device_id",
            "source_session_id",
            "recall_session_id",
        )
    ):
        raise ValueError("Project link metadata differs from authenticated Server")
    output = project_settings(args.device_home, args.project_root)
    if output.exists():
        raise ValueError(
            "Existing project setup preserved; use another explicitly selected local project"
        )
    args.output = str(output)
    args.server_bundle = None
    setup(args)
    p = private_json(output)
    recall = dict(
        schema=2,
        enabled=True,
        lifecycle="until-disabled",
        device_id=client.device_id,
        profile_sha256=r["https_profile_sha256"],
        max_results=5,
        off_file=p["off_file"],
        **{
            k: link[k]
            for k in (
                "actor",
                "endpoint",
                "project_id",
                "source_mode",
                "source_device_id",
                "source_session_id",
                "recall_session_id",
            )
        },
    )
    path = output.parent / "linked-recall.json"
    pin = write(path, recall, exclusive=True)
    p.update(
        source_mode="recall-only", recall_policy=str(path), recall_policy_sha256=pin
    )
    pin = write(output, p)
    p = load(output, pin)
    return dict(
        install(p, str(output), pin),
        source_mode="recall-only",
        source_permissions_added=False,
    )
