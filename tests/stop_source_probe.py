"""Real CLI controls and mixed-source preparation in isolated synthetic homes."""
import importlib.abc
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]


def worker(mode):
    sys.path[:0] = [str(SOURCE / "src"), str(SOURCE / "tests"), str(SOURCE)]
    def deny_external(event, args):
        if event.startswith("socket.") or event == "subprocess.Popen":
            raise AssertionError("No network, credentials helper, or worker process in audit")
    sys.addaudithook(deny_external)
    from agent_brain_client.adapters.storage import metadata, registration
    from agent_brain_client.adapters.storage import project_settings as ps
    from agent_brain_client.adapters.storage.collection import settings_path
    from agent_brain_client.adapters.packages.layout import PYTHON_FILES
    from agent_brain_client.adapters.platforms.filesystem import private_directory
    home = private_directory(Path.home().resolve())
    content = {"schema": 1, "adapter": "codex", "kinds": ["memory", "rule", "skill"],
               "ids": {"memory": "*", "rule": ["synthetic-rule"], "skill": ["synthetic-skill"]},
               "dependencies": {"commands": [], "python_modules": []}}
    policy = home / "content.json"
    policy_sha = metadata.write(policy, content)
    profile = home / "profile.json"
    profile_sha = metadata.write(profile, {"synthetic": True})
    release = home / "release.json"
    release_sha = metadata.write(release, {"synthetic": True})
    runtime = {"codex_home": str(home / ".config/agent-brain-client/shared-native"),
               "content_cache": str(home / "cache"), "content_policy": str(policy),
               "content_policy_sha256": policy_sha, "https_profile": str(profile),
               "https_profile_sha256": profile_sha}
    enrolled = {"runtime": runtime}
    meta = {"release_policy": str(release), "release_policy_sha256": release_sha}
    codex_reg = home / ".config/agent-brain-client/codex/registration.json"
    private_directory(codex_reg.parent)
    metadata.write(codex_reg, {"schema": 1, "native_home": str(home / ".codex"), "mcp": {}, "removed": False})
    pins = {name: metadata.digest((SOURCE / name).read_bytes()) for name in PYTHON_FILES}
    records, rows = [], []
    for index in range(2):
        project = home / ("project-a" if index == 0 else "project-b")
        private_directory(project)
        harness = "codebuddy" if mode.endswith("codebuddy") or mode.startswith("mixed") and index == 1 else "codex"
        file = ps.project_settings(home, project, harness=harness)
        private_directory(file.parent)
        off = file.parent / "memory.off"
        recall = file.parent / "recall.json"
        recall_sha = metadata.write(recall, {"schema": 2, "lifecycle": "until-disabled", "enabled": True,
            "project_id": "synthetic-project-" + str(index), "recall_session_id": "synthetic-reader"})
        cap = file.parent / "capture.json"
        cap_sha = metadata.write(cap, {"schema": 2, "enabled": True, "lifecycle": "until-disabled",
            "source_mode": "codex-project", "notes_directory": None,
            "profile_sha256": profile_sha, "content_policy_sha256": policy_sha, "off_file": str(off),
            "scopes": [{"cwd": str(project), "project_id": "synthetic-project-" + str(index), "session_id": None}]})
        native = home / (".codex" if harness == "codex" else ".codebuddy")
        cache = home / ".config/agent-brain-client/codex/content-cache" if harness == "codex" else home / ".config/agent-brain-client/codebuddy/project-outboxes" / metadata.digest(str(project).encode())
        record = {"schema": 1, "harness": harness, "enabled": True, "project_root": str(project),
            "device_home": str(home), "codex_home": str(native), "profile": str(profile), "profile_sha256": profile_sha,
            "content_policy": str(policy), "content_policy_sha256": policy_sha,
            "release_policy": str(release), "release_policy_sha256": release_sha, "cache": str(cache),
            "off_file": str(off), "query": "", "max_items": 3, "max_context_bytes": 12288,
            "code_sha256": pins, "capture_policy": str(cap), "capture_policy_sha256": cap_sha,
            "recall_policy": str(recall), "recall_policy_sha256": recall_sha, "source_mode": "codex-project"}
        if mode == "mixed-legacy" and harness == "codex":
            record.pop("harness")
        pin = metadata.write(file, record)
        records.append(record)
        rows.append({"root": str(project), "settings": str(file), "settings_sha256": pin})

    settings = {"schema": 1, "enabled": False, "device_home": str(home),
        "scope": {"schema": 1, "profile_id": "a" * 32, "include_roots": [], "exclude_roots": [],
                  "non_project_roots": [], "recall_personal": True},
        "approved_scope_sha256": None, "server_scope_sha256": None,
        "legacy_projects": rows, "code_sha256": pins, "operator": None, "operator_sha256": None}
    file = settings_path(home)
    private_directory(file.parent)
    metadata.write(file, settings)
    if mode.startswith("mixed"):
        # Source preparation should discover the retained route, not bind a native runtime.
        metadata.write(file, dict(settings, legacy_projects=[]))
        attempts = []
        class Guard(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.startswith("agent_brain_client.adapters.harnesses."):
                    attempts.append(fullname)
                    raise AssertionError("Concrete Harness in shared source setup: " + fullname)
        sys.meta_path.insert(0, Guard())
        from agent_brain_client.runtime import host_maintenance
        args = SimpleNamespace(device_home=str(home), harness="codebuddy", profile_id=None, operator=None, operator_sha256=None)
        with patch.object(registration, "registration", return_value=(meta, enrolled)), patch.object(ps, "registration", return_value=(meta, enrolled)), patch.object(host_maintenance, "registration", return_value=(meta, enrolled)), patch("agent_brain_client.adapters.installation.installation.registered_context", return_value=(None, {"args": ["--harness", "codebuddy"]}, None, None)):
            result = host_maintenance.install_sources(args)
            assert result["status"] == "shared-source-runtime-prepared", result
            assert [r["root"] for r in metadata.private_json(file)["legacy_projects"]] == ([rows[0]["root"]] if os.name != "nt" else [])
            # Reverse selected host: shared preparation still validates both
            # metadata records without binding either native implementation.
            from agent_brain_client.adapters.storage.collection_setup import prepare
            args.harness = "codex"
            metadata.write(file, dict(settings, legacy_projects=[]))
            prepared = prepare(args, locate=lambda *a: (_ for _ in ()).throw(AssertionError("Native location requested")))
            assert [r["root"] for r in prepared["legacy_projects"]] == ([rows[0]["root"]] if os.name != "nt" else [])
            # Keep full pin/identity validation and strict host schema checks.
            for mutation in ({"profile_sha256": "0" * 64}, {"harness": "unknown"}, {"extra": True}, {"codex_home": str(home / "wrong-native")}, {"code_sha256": dict(pins, **{next(iter(pins)): "0" * 64})}):
                metadata.write(Path(rows[0]["settings"]), dict(records[0], **mutation))
                metadata.write(file, dict(settings, legacy_projects=[]))
                assert prepare(args, locate=registration.content_location)["legacy_projects"] == []
        assert not attempts, attempts
        assert not (home / ".codex").exists() and not (home / ".codebuddy").exists()
        return {"mode": mode, "foreign_imports": attempts, "valid_legacy_route_retained": True}

    from agent_brain_client.runtime import project_memory
    from agent_brain_client.interfaces.cli import collection, project as project_cli
    requests = []
    failure = "transport"
    class Cloud:
        grant = {"scopes": ["read", "put"], "artifact_types": content["kinds"], "ids": content["ids"]}
        queue_identity = {"service": "synthetic-audit"}
        def request(self, request):
            requests.append(request)
            is_a = request["params"].get("project_id") == "synthetic-project-0"
            if request["method"] == "memory_stop" and is_a and mode.startswith("stop-partial"):
                assert all(Path(p["off_file"]).exists() for p in records), "Remote request before every local fence"
                raise OSError("Synthetic first-project transport unavailable")
            if is_a and mode.startswith("resume"):
                if failure == "transport": raise OSError("Synthetic transport unavailable")
                if failure == "error": return {"error": {"code": "synthetic-denial"}}
                if failure == "missing": return {}
                if failure == "malformed": return {"result": {"schema": 2}}
                model, source = failure
            else:
                model = source = request["method"] != "memory_stop"
            return {"result": {"schema": 2, "lifecycle": "until-disabled", "model_admitted": model, "source_admitted": source}}
    cloud = Cloud()
    result_rows = []
    with patch.object(registration, "registration", return_value=(meta, enrolled)), patch.object(ps, "registration", return_value=(meta, enrolled)), patch.object(project_memory, "registration", return_value=(meta, enrolled)), patch("agent_brain_client.adapters.server.https.HTTPSContentClient", return_value=cloud):
        # These are real pinned settings and the public collection/project CLI load paths.
        for row in rows:
            assert project_memory.load(row["settings"], row["settings_sha256"])["enabled"]
        if mode.startswith("approved-"):
            from agent_brain_client.domain.scopes import digest as scope_digest
            scope = dict(settings["scope"], include_roots=[{"root": str(home), "sharing": "personal"}])
            approved = dict(settings, enabled=True, scope=scope,
                            approved_scope_sha256=scope_digest(scope), server_scope_sha256=scope_digest(scope))
            metadata.write(file, approved)
            sentinels = [Path(p["off_file"]).with_name("global-memory.off") for p in records]
            outcome, resume_reply = "success", {"result": {"schema": 1, "stopped": True}}
            class GlobalControl:
                def __init__(self, value):
                    pass
                def wire(self, method, params):
                    if method == "memory_global_stop":
                        assert all(p.exists() for p in sentinels), "Remote global stop before local project fences"
                        if outcome == "transport":
                            raise OSError("Synthetic global transport unavailable")
                        return {"result": {"schema": 1, "stopped": True}}
                    assert method == "memory_global_resume"
                    return resume_reply
            def cli(action):
                out = io.StringIO()
                with patch.object(collection.core, "CollectionClient", GlobalControl), patch.object(sys, "stdout", out):
                    rc = collection.main([action, "--device-home", str(home)])
                return rc, json.loads(out.getvalue())
            for outcome in ("success", "transport"):
                for record in records:
                    Path(record["off_file"]).unlink(missing_ok=True)
                metadata.write(Path(records[0]["off_file"]), {"synthetic": "independent-project-stop"})
                original = Path(records[0]["off_file"]).read_bytes()
                rc, value = cli("global-stop")
                assert rc == (0 if outcome == "success" else 2), value
                assert all(p.exists() for p in sentinels)
                assert not any(ps.active(p) for p in records)
                resume_reply = {"result": {"schema": 1, "stopped": True}}
                rc, value = cli("global-resume")
                assert rc == 2 and not value["remote_confirmed"], value
                assert all(p.exists() for p in sentinels)
                # A separate project stop made during the global stop survives
                # the later global resume as well as a stop that predated it.
                if outcome == "transport":
                    with patch.object(sys, "argv", ["project.py", "stop", "--harness", records[1]["harness"], "--settings", rows[1]["settings"], "--settings-sha256", rows[1]["settings_sha256"]]), patch.object(sys, "stdout", io.StringIO()):
                        assert project_cli.main() == 0
                resume_reply = {"result": {"schema": 1, "stopped": False}}
                rc, value = cli("global-resume")
                assert rc == 0 and value["collection_enabled"], value
                assert not any(p.exists() for p in sentinels)
                assert Path(records[0]["off_file"]).read_bytes() == original
                assert ps.active(records[1]) == (outcome == "success")
            # If source reconfiguration returns to legacy control, confirmed
            # project resume must also release a previous global-only fence.
            for sentinel in sentinels:
                metadata.write(sentinel, {"schema": 1, "control": "global-stop"})
            metadata.write(file, settings)
            rc, value = cli("global-resume")
            assert rc == 0 and value["legacy_capture_resumed"] and not any(p.exists() for p in sentinels), value
            metadata.write(file, approved)
            metadata.write(sentinels[1], {"synthetic": "unknown-marker"})
            before = sentinels[1].read_bytes()
            rc, value = cli("global-resume")
            assert rc == 2 and value["remote_confirmed"] and not value["collection_enabled"], value
            assert sentinels[1].read_bytes() == before and (file.parent / "global.off").exists()
            return {"mode": mode, "global_stop_resume_restored": True, "independent_stops_preserved": True}
        if mode.startswith("resume"):
            for failure in ((False, False), (False, True), (True, False), (True, True), "malformed", "missing", "error", "transport"):
                for reverse in (False, True):
                    requests.clear()
                    for record in records: metadata.write(Path(record["off_file"]), {"synthetic": "preserve-on-denial"})
                    original_marker = Path(records[0]["off_file"]).read_bytes()
                    metadata.write(file.parent / "global.off", {})
                    metadata.write(file, dict(settings, legacy_projects=rows[::-1] if reverse else rows))
                    output = io.StringIO()
                    with patch.object(sys, "stdout", output):
                        rc = collection.main(["global-resume", "--device-home", str(home), "--harness", records[0]["harness"]])
                    value = json.loads(output.getvalue())
                    confirmed = failure == (True, True)
                    assert rc == (0 if confirmed else 2), value
                    assert value["remote_confirmed"] is confirmed
                    assert (file.parent / "global.off").exists() is not confirmed
                    assert Path(records[0]["off_file"]).exists() is not confirmed
                    if not confirmed: assert Path(records[0]["off_file"]).read_bytes() == original_marker
                    assert not Path(records[1]["off_file"]).exists()
                    assert len(requests) == 2
                    result_rows.append({"failure": failure, "reverse": reverse, "rc": rc,
                        "status": value["status"], "global_fence": not confirmed,
                        "failed_project_marker": not confirmed, "confirmed_project_marker": False})
            return {"mode": mode, "cases": result_rows, "real_cli_load_and_content_session": True}
        # Exercise the real loader, settings pins, CLI and HTTPS request boundary.
        for reverse in (False, True):
            requests.clear()
            for record in records:
                Path(record["off_file"]).unlink(missing_ok=True)
            gates = []
            with patch("agent_brain_client.adapters.storage.observed_migration.migrate_staged_turns"):
                for row in rows:
                    gates.append(project_memory.delivery_port(row["settings"], row["settings_sha256"])[1:])
            metadata.write(file, dict(settings, legacy_projects=rows[::-1] if reverse else rows))
            output = io.StringIO()
            with patch.object(sys, "stdout", output):
                rc = collection.main(["global-stop", "--device-home", str(home)])
            stop_result = json.loads(output.getvalue())
            assert rc == 2 and stop_result["status"] == "global-memory-stop-unconfirmed", stop_result
            assert not stop_result["remote_confirmed"] and stop_result["local_stop_fence"]
            assert (file.parent / "global.off").exists()
            assert all(Path(record["off_file"]).exists() for record in records)
            assert len(requests) == 2 and all(r["method"] == "memory_stop" for r in requests), requests
            for row in stop_result["projects"]:
                assert row["local_stop_fence"]
                assert row["remote_confirmed"] == (row["root"] == records[1]["project_root"]), row
                if not row["remote_confirmed"]:
                    assert row["remote_error_type"] == "OSError", row
            # Stopped hooks cannot enqueue and original delivery gates reject
            # both sessions and events. No permission predicate is mocked.
            for index, record in enumerate(records):
                before = len(list((Path(record["cache"]) / "outbox").glob("*.json")))
                hook_out, hook_err = io.StringIO(), io.StringIO()
                payload = {"cwd": record["project_root"], "hook_event_name": "UserPromptSubmit",
                    "session_id": "synthetic-session", "turn_id": "synthetic-turn-after-global-stop",
                    "prompt": "Synthetic prompt after a failed global stop"}
                with patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))), patch.object(sys, "stdout", hook_out), patch.object(sys, "stderr", hook_err), patch("agent_brain_client.runtime.delivery.schedule_project"), patch.object(project_memory, "recalled", return_value=[]), patch.object(sys, "argv", ["project.py", "hook", "--harness", record["harness"], "--settings", rows[index]["settings"], "--settings-sha256", rows[index]["settings_sha256"]]):
                    hook_rc = project_cli.main()
                outbox = list((Path(record["cache"]) / "outbox").glob("*.json"))
                assert hook_rc == 0 and len(outbox) == before, (hook_out.getvalue(), hook_err.getvalue())
                allowed, allowed_event = gates[index]
                assert not allowed("synthetic-session")
                assert not allowed_event({"session_id": "synthetic-session", "project_id": "synthetic-project-" + str(index), "messages": []})
                try:
                    project_memory.delivery_port(rows[index]["settings"], rows[index]["settings_sha256"])
                except ValueError as error:
                    assert str(error) == "Project source inactive"
                else:
                    raise AssertionError("Stopped delivery worker was admitted")
        return {"mode": mode, "global_stop_rc": rc, "result": stop_result,
                "new_events_after_stop": 0, "delivery_admitted": False, "network_disabled": True}



if __name__ == "__main__":
    print(json.dumps(worker(sys.argv[1])))
