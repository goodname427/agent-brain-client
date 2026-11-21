"""CodeBuddy project Hook targets and native turn normalization."""

from pathlib import Path
from agent_brain_client.adapters.harnesses.codebuddy.configuration import read_host, render_json
from agent_brain_client.adapters.harnesses.codebuddy.events import session_id
from agent_brain_client.adapters.storage import metadata
from agent_brain_client.adapters.storage.host_files import host_plan, host_apply, host_restore
from agent_brain_client.adapters.platforms.filesystem import locked

def definitions(settings, path):
    from agent_brain_client.adapters.installation import launcher_update as hook_launcher

    command = hook_launcher.native_hook_command(settings["device_home"], "codebuddy", path)
    return {
        event: {"hooks": [{"type": "command", "command": command, "timeout": 90}]}
        for event in ("SessionStart", "UserPromptSubmit", "Stop")
    }


def install(settings, path, pin):
    from agent_brain_client.adapters.installation import launcher_update as hook_launcher
    if settings["harness"] != "codebuddy":
        raise ValueError("CodeBuddy project binding required")
    hook_launcher.prepare_native_hook(settings["device_home"])
    target = metadata.canonical(Path(settings["project_root"]) / ".codebuddy/settings.local.json")
    directory = Path(path).parent
    journal = directory / "codebuddy-hooks-install.json"
    with locked(directory / "codebuddy-hooks.lock"):
        previous = metadata.private_json(journal) if journal.exists() else None
        if previous and previous.get("phase") == "applying":
            if set(previous.get("host", {})) != {str(target)}:
                raise ValueError("Project recovery target changed")
            host_restore(previous["host"], lambda: metadata.write(journal, previous))
            previous = dict(previous, phase="rolled-back")
            metadata.write(journal, previous)
        groups = definitions(settings, path)
        old = None
        if previous:
            old = previous.get("definitions" if previous["phase"] == "complete" else "previous_definitions")
        desired = render_json(read_host(target), groups, old, hooks=True)
        record = {"schema": 1, "phase": "applying", "settings_sha256": pin,
                  "target": str(target), "definitions": groups, "previous_definitions": old,
                  "host": host_plan({target: desired})}
        changed = any(row["before_sha"] != row["target_sha"] for row in record["host"].values())
        if (not changed and previous and previous["phase"] == "complete"
                and previous.get("settings_sha256") == pin):
            return {"status": "project-memory-installed", "harness": "codebuddy", "native_changed": False,
                    "source_mode": settings["source_mode"], "model_loaded": "unknown"}
        metadata.write(journal, record)
        try:
            host_apply(record["host"], lambda: metadata.write(journal, record))
            metadata.write(journal, dict(record, phase="complete"))
        except Exception:
            host_restore(record["host"], lambda: metadata.write(journal, record))
            metadata.write(journal, dict(record, phase="rolled-back"))
            raise
    return {"status": "project-memory-installed", "harness": "codebuddy", "native_changed": changed,
            "source_mode": settings["source_mode"], "model_loaded": "unknown"}


def normalize(settings, payload):
    from agent_brain_client.adapters.harnesses.codebuddy import transcript
    if payload.get("cwd") != settings["project_root"]:
        raise ValueError("Exact project origin required")
    kind = payload.get("hook_event_name")
    if kind == "SessionStart":
        return payload
    generation = payload.get("generation_id")
    if not isinstance(generation, str) or not generation:
        raise ValueError("Stable CodeBuddy generation required")
    result = dict(payload, session_id=session_id(payload.get("session_id")), turn_id="native_" + metadata.digest(generation.encode()))
    if kind == "UserPromptSubmit":
        transcript.observe(settings["device_home"], payload, None)
    elif kind == "Stop":
        if payload.get("stop_hook_active") is not False:
            raise ValueError("Recursive CodeBuddy project Stop refused")
        messages, row, file = transcript.assistant(settings["device_home"], payload, None)
        result["_project_marker"] = str(file)
        result["_project_turn_skipped"] = not messages and (row["excluded"] or row["complete"])
        result["last_assistant_message"] = "\n".join(message["content"] for message in messages)
        result["stop_hook_active"] = False
    return result


def register_completion(settings, payload, path, pin):
    from agent_brain_client.adapters.harnesses.codebuddy.transcript import marker
    from agent_brain_client.adapters.platforms.filesystem import cache_locked
    if payload.get("cwd") != settings["project_root"] or payload.get("stop_hook_active") is not False:
        raise ValueError("Exact nonrecursive project completion origin required")
    file = marker(settings["device_home"], payload)
    with cache_locked(file.with_suffix(".lock"), timeout=1):
        row = metadata.private_json(file, cache=True)
        if any(row["binding"].get(key) != payload.get(key) for key in ("session_id", "generation_id", "cwd")):
            raise ValueError("Observed project turn changed")
        wanted = {"settings": str(path), "settings_sha256": pin}
        if row.get("project_delivery", wanted) != wanted:
            raise ValueError("Observed turn project destination changed")
        row["project_delivery"] = wanted
        metadata.write(file, row, cache=True)
    return file.stem


def complete_observation(payload, result):
    if not payload.get("_project_marker"):
        return
    if not result.get("local_queue", {}).get("saved") and result.get("status") not in ("excluded-turn", "project-turn-skipped"):
        return
    from agent_brain_client.adapters.platforms.filesystem import cache_locked
    file = Path(payload["_project_marker"])
    with cache_locked(file.with_suffix(".lock"), timeout=1):
        row = metadata.private_json(file, cache=True)
        row["complete"] = True
        metadata.write(file, row, cache=True)
