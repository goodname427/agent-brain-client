"""Real public entry regressions in new processes with only synthetic private state."""

import contextlib
import importlib.abc
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
POLICY = {"schema": 1, "adapter": "codex", "kinds": ["memory", "rule", "skill"],
          "ids": {"memory": "*", "rule": ["synthetic-rule"], "skill": ["synthetic-skill"]},
          "dependencies": {"commands": [], "python_modules": []}}


class HostEntries(unittest.TestCase):
    def run_probe(self, mode, harness):
        with tempfile.TemporaryDirectory(prefix="synthetic-entry-home-") as tmp:
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(Path(__file__).resolve()), mode, harness],
                env=dict(os.environ, HOME=str(Path(tmp) / "home"), USERPROFILE=str(Path(tmp) / "home"), CODEX_HOME=str(Path(tmp) / "home/.codex")),
                capture_output=True, text=True, timeout=40,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"passed": mode, "harness": harness})

    def test_real_mcp_and_mapping_main_forbid_the_other_host(self):
        for harness in ("codex", "codebuddy"):
            for mode in ("mcp", "mapping"):
                with self.subTest(harness=harness, mode=mode):
                    self.run_probe(mode, harness)

    def test_real_global_hook_cli_routes_each_host(self):
        for harness in ("codex", "codebuddy"):
            with self.subTest(harness=harness):
                self.run_probe("global-hook", harness)

    def test_mcp_event_main_preserves_codex_and_rejects_unsupported_host(self):
        for harness in ("codex", "codebuddy"):
            with self.subTest(harness=harness):
                self.run_probe("event", harness)

    def test_nonempty_project_initialization_and_legacy_cli_controls(self):
        for harness in ("codex", "codebuddy"):
            with self.subTest(harness=harness):
                self.run_probe("projects", harness)


def probe(mode, harness):
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
    other = "codex" if harness == "codebuddy" else "codebuddy"
    attempts = []

    class ForbiddenHost(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith("agent_brain_client.adapters.harnesses." + other):
                attempts.append(fullname)
                raise AssertionError("Foreign Harness imported: " + fullname)

    sys.meta_path.insert(0, ForbiddenHost())
    from agent_brain_client.adapters.storage import metadata, registration
    from agent_brain_client.runtime.harness_content import content_host
    from agent_brain_client.adapters.platforms.filesystem import private_directory, private_mode
    home = private_directory(Path.home().resolve())
    native = home / (".codex" if harness == "codex" else ".codebuddy")
    content_policy = home / "content.json"
    content_pin = metadata.write(content_policy, POLICY)

    class Cloud:
        grant = {"scopes": ["read", "put"], "artifact_types": POLICY["kinds"], "ids": POLICY["ids"]}
        queue_identity = {"service": "synthetic-entry"}
        profile_sha = "c" * 64
        profile = {"platform": "darwin-arm64", "state_directory": str(home / ".config/agent-brain-client/https")}
        actor = "synthetic-actor"

        def request(self, request):
            if request["method"] == "memory_event_submit":
                return {"request_id": request["request_id"], "result": {
                    "event_id": request["params"]["id"], "state": "committed",
                    "message_ids": ["synthetic-msg-" + str(i) for i in range(len(request["params"]["event"]["messages"]))],
                    "core_acceptance": "acknowledged-not-durability-proof", "ledger_sync": "sqlite-wal-full",
                    "pipeline_state": "unverified", "model_extraction_verified": False},
                    "persistence": {"state": "core-accepted", "durable": False, "github_confirmed": False, "receipt_ledger_durable": True}}
            return {"request_id": request["request_id"], "result": {"content_api_version": 1, "commit": "a" * 40, "artifacts": []}}

        def activate_identity(self):
            pass

    if mode in ("mcp", "mapping", "event"):
        from agent_brain_client.interfaces.mcp import content
        signing = home / "signing.json"
        signing_pin = metadata.write(signing, {"synthetic": True})
        argv = ["content.py", "--harness", harness, "--content-policy", str(content_policy),
                "--content-policy-sha256", content_pin, "--device-home", str(home), "--cache", str(home / "cache")]
        if harness == "codex":
            argv += ["--codex-home", str(native)]
        if mode == "event":
            argv += ["--event-hook", "--https-profile", str(home / "synthetic-profile.json"),
                     "--https-profile-sha256", Cloud.profile_sha]
            hook = {"schema": 1, "enabled": True, "profile_sha256": Cloud.profile_sha,
                    "content_policy_sha256": content_pin, "off_file": str(home / "capture.off"),
                    "scopes": [{"cwd": str(home), "project_id": "synthetic-project", "session_id": "synthetic-session"}]}
            hook_file = home / "hook.json"
            hook_pin = metadata.write(hook_file, hook)
            argv += ["--event-hook-policy", str(hook_file), "--event-hook-policy-sha256", hook_pin]
            if harness == "codebuddy":
                class Unreadable:
                    def read(self, *args):
                        raise AssertionError("Unsupported event mode read stdin")
                with patch.object(sys, "argv", argv), patch.object(sys, "stdin", SimpleNamespace(buffer=Unreadable())), patch("agent_brain_client.adapters.server.https.HTTPSContentClient", side_effect=AssertionError("Credentials accessed")):
                    try:
                        content.main()
                    except ValueError as error:
                        assert "does not support" in str(error), str(error)
                    else:
                        raise AssertionError("Unsupported MCP event mode accepted")
                assert not native.exists()
            else:
                source = ROOT / "src/agent_brain_client/adapters/platforms/macos/native/CredentialStore.swift"
                source_pin = metadata.digest(source.read_bytes())
                Cloud.profile["credential_source_sha256"] = source_pin
                argv += ["--credential-source-sha256", source_pin]
                for kind in ("UserPromptSubmit", "Stop"):
                    payload = {"cwd": str(home), "session_id": "synthetic-session", "turn_id": "synthetic-turn",
                               "hook_event_name": kind, "prompt": "Synthetic preference", "stop_hook_active": False,
                               "last_assistant_message": "Synthetic final answer"}
                    out = io.StringIO()
                    with patch.object(sys, "argv", argv), patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))), patch.object(sys, "stdout", out), patch("agent_brain_client.adapters.server.https.HTTPSContentClient", return_value=Cloud()):
                        content.main()
                    assert json.loads(out.getvalue()) == {}
                rows = [metadata.private_json(p, cache=True) for p in (home / "cache/outbox").glob("*.json")]
                assert len(rows) == 1 and rows[0]["state"] == "confirmed", rows
        else:
            argv += ["--signing-policy", str(signing), "--signing-policy-sha256", signing_pin,
                     "--device-plan", str(home / "synthetic-plan.json"), "--device-plan-sha256", "b" * 64]
            if mode == "mapping":
                argv += ["--mapping-refresh"]
            message = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "synthetic-entry", "version": "1"}}}
            out = io.StringIO()
            with patch.object(sys, "argv", argv), patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO((json.dumps(message) + "\n").encode()))), patch.object(sys, "stdout", out), patch.object(content, "ComponentClient", return_value=Cloud()), patch("agent_brain_client.runtime.delivery.supervise", return_value=threading.Event()):
                content.main()
            result = json.loads(out.getvalue())
            assert (result["status"] if mode == "mapping" else result["result"]["content_mapping"]["status"]) == "content-refreshed", result
            assert native.exists()
    elif mode == "global-hook":
        from agent_brain_client.interfaces.cli import collection
        core = collection.core
        value = {"device_home": str(home)}
        local = {"kind": "non-project", "cwd": str(home), "project_root": None, "repository_root": None}
        staged, scheduled, recalls = [], [], []
        # Native parsing/routing/lifecycle remain real; fake only service I/O.
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(core, "load", return_value=value))
            stack.enter_context(patch.object(core, "approved", return_value=True))
            stack.enter_context(patch.object(core, "eligible", return_value=local))
            stack.enter_context(patch.object(core, "legacy", return_value=None))
            stack.enter_context(patch.object(core, "schedule_delivery", side_effect=lambda *a: scheduled.append(a)))
            stack.enter_context(patch.object(core, "recall_prompt", side_effect=lambda *a, **kw: recalls.append(a) or [{"content": "Synthetic recalled preference"}]))
            if harness == "codex":
                stack.enter_context(patch.object(core, "submit_hook", side_effect=lambda *a: staged.append(a) or {"status": "staged"}))
            else:
                from agent_brain_client.adapters.harnesses.codebuddy import events, transcript
                stack.enter_context(patch.object(events, "capture_messages", side_effect=lambda *a: staged.append(a) or {"status": "staged"}))
                stack.enter_context(patch.object(transcript, "observe", return_value="unavailable"))
                stack.enter_context(patch.object(events, "finish_assistant", side_effect=lambda *a: staged.append(a) or {"status": "staged"}))
            for kind in ("SessionStart", "UserPromptSubmit", "Stop"):
                payload = {"cwd": str(home), "session_id": "synthetic-session", "turn_id": "synthetic-turn", "generation_id": "synthetic-generation",
                           "hook_event_name": kind, "prompt": "Synthetic preference", "stop_hook_active": False}
                out, err = io.StringIO(), io.StringIO()
                with patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))), patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
                    rc = collection.main(["global-hook", "--harness", harness, "--device-home", str(home)])
                assert rc == 0 and "review-required" not in err.getvalue(), err.getvalue()
                result = json.loads(out.getvalue())
                if kind == "UserPromptSubmit":
                    assert "Synthetic recalled preference" in json.dumps(result), result
                else:
                    assert result == {}, result
        assert len(staged) == 2 and len(scheduled) == 3 and len(recalls) == 1, (staged, scheduled, recalls)
    elif mode == "projects":
        from agent_brain_client.runtime import project_memory, host_maintenance
        from agent_brain_client.interfaces.cli import collection
        from agent_brain_client.adapters.storage.collection import settings_path
        from agent_brain_client.adapters.storage.project_settings import project_settings
        project = home / "synthetic-project"
        project.mkdir()
        profile, release = home / "profile.json", home / "release.json"
        profile_pin, release_pin = metadata.write(profile, {"synthetic": True}), metadata.write(release, {"synthetic": True})
        runtime = {"codex_home": str(home / ".codex"), "content_cache": str(home / "cache"),
                   "content_policy": str(content_policy), "content_policy_sha256": content_pin,
                   "https_profile": str(profile), "https_profile_sha256": profile_pin}
        meta, enrolled = {"release_policy": str(release), "release_policy_sha256": release_pin}, {"runtime": runtime}
        output = project_settings(home, project, harness=harness)
        args = SimpleNamespace(device_home=str(home), project_root=str(project), harness=harness, output=str(output), query="", server_bundle=None,
                               profile_id=None, operator=None, operator_sha256=None)
        with patch.object(registration, "registration", return_value=(meta, enrolled)), patch.object(project_memory, "registration", return_value=(meta, enrolled)), patch("agent_brain_client.adapters.storage.project_settings.registration", return_value=(meta, enrolled)), patch.object(host_maintenance, "registration", return_value=(meta, enrolled)), patch("agent_brain_client.adapters.installation.installation.registered_context", return_value=(None, {"args": ["--harness", "codebuddy"]}, None, None)):
            setup = project_memory.setup(args)
            loaded = project_memory.load(output, setup["settings_sha256"])
            assert loaded["harness"] == harness
            # Real shared setup path with a nonempty project directory.
            result = host_maintenance.install_sources(SimpleNamespace(device_home=str(home), harness="codebuddy", profile_id=None, operator=None, operator_sha256=None))
            assert result["status"] == "shared-source-runtime-prepared", result
            shared = metadata.private_json(settings_path(home))
            assert shared["legacy_projects"] == [] and shared["enabled"] is False
            # Retain old schema-1 Codex records and a real pinned source grant.
            cap, recall = home / "capture.json", home / "recall.json"
            cap_pin = metadata.write(cap, {"synthetic": True})
            recall_pin = metadata.write(recall, {"schema": 2, "project_id": "synthetic-project", "recall_session_id": "synthetic-reader"})
            loaded.update(source_mode="codex-project", capture_policy=str(cap), capture_policy_sha256=cap_pin,
                          recall_policy=str(recall), recall_policy_sha256=recall_pin)
            if harness == "codex":
                loaded.pop("harness")
            pin = metadata.write(output, loaded)
            from agent_brain_client.adapters.storage.collection_setup import prepare
            prepared = prepare(args, locate=lambda home, runtime, host: content_host(host).location(home, runtime))
            assert len(prepared["legacy_projects"]) == (1 if harness == "codex" and os.name != "nt" else 0), prepared
            value = dict(shared, legacy_projects=[{"settings": str(output), "settings_sha256": pin}], server_scope_sha256=None)
            calls = []
            class ControlClient:
                def request(self, request):
                    calls.append(request["method"])
                    return {"result": {"schema": 2, "lifecycle": "until-disabled", "model_admitted": True, "source_admitted": True}}
            with patch.object(collection.core, "load", return_value=value), patch.object(project_memory, "adapter_for", return_value=(ControlClient(), None)):
                for action in ("stop", "resume"):
                    out = io.StringIO()
                    with patch.object(sys, "stdout", out):
                        rc = collection.main(["global-" + action, "--device-home", str(home)])
                    result = json.loads(out.getvalue())
                    assert rc == 0 and result["remote_confirmed"], result
                    assert (settings_path(home).parent / "global.off").exists() == (action == "stop")
                    assert Path(loaded["off_file"]).exists() == (action == "stop")
                    if action == "stop":
                        private_mode(loaded["off_file"], owner=True)
                marker = Path(loaded["off_file"])
                metadata.write(marker, {"synthetic": "retained-stop"}, exclusive=True)
                before = marker.read_bytes()
                with patch.object(sys, "stdout", io.StringIO()):
                    assert collection.main(["global-stop", "--device-home", str(home)]) == 0
                assert marker.read_bytes() == before
                marker.unlink()
                with patch.object(metadata, "write", side_effect=OSError("Synthetic marker write failure")), patch.object(sys, "stdout", io.StringIO()):
                    assert collection.main(["global-stop", "--device-home", str(home)]) == 2
                assert calls == ["memory_stop", "memory_resume", "memory_stop"], calls
                with patch.object(project_memory, "adapter_for", side_effect=OSError("Synthetic remote unavailable")), patch.object(sys, "stdout", io.StringIO()):
                    assert collection.main(["global-resume", "--device-home", str(home)]) == 2
                assert (settings_path(home).parent / "global.off").exists()
            assert calls == ["memory_stop", "memory_resume", "memory_stop"], calls
    else:
        raise AssertionError(mode)
    assert not attempts, attempts
    assert not any(n.startswith("agent_brain_client.adapters.harnesses." + other) for n in sys.modules)
    assert not (home / (".codex" if harness == "codebuddy" else ".codebuddy")).exists() if mode != "projects" else True
    print(json.dumps({"passed": mode, "harness": harness}))


if __name__ == "__main__":
    if len(sys.argv) == 3:
        probe(*sys.argv[1:])
    else:
        unittest.main()
