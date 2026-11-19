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


@unittest.skipUnless(sys.platform in ("darwin", "win32"), "Native Client entry contracts require macOS or Windows")
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

    def test_real_stop_failures_and_mixed_source_isolation(self):
        modes = ("mixed-current", "mixed-legacy", "resume-codex", "resume-codebuddy", "stop-partial-codex", "stop-partial-codebuddy", "approved-codex", "approved-codebuddy")
        for mode in modes:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="synthetic-stop-source-") as tmp:
                home = Path(tmp) / "home"
                # The Client creates HOME with the exact native owner/DACL.
                result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "tests/stop_source_probe.py"), mode],
                    env=dict(os.environ, HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / ".codex")),
                    capture_output=True, text=True, timeout=40)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["mode"], mode)



class _RolloutTestQueue:
    """Filesystem-backed queue fixture with no native platform or transport."""

    def __init__(self, cache):
        self.cache = cache
        cache.mkdir(exist_ok=True)

    def _outbox_lock(self, *_):
        return contextlib.nullcontext()

    def _load(self, name):
        path = self.cache / name
        return json.loads(path.read_bytes()) if path.exists() else None

    def _save(self, name, value):
        path = self.cache / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def _safe(self, path, base):
        path.relative_to(base)

    def _discard_staging(self, name):
        (self.cache / name).unlink()

    def _enqueue(self, request):
        name = "outbox/" + request["request_id"] + ".json"
        row = self._load(name)
        if row is None:
            row = {"state": "pending", "request": request}
            self._save(name, row)
        elif row["request"] != request:
            raise ValueError("Synthetic request changed")
        return row

    def messages(self):
        return sorted(
            (message["role"], message["content"])
            for file in (self.cache / "outbox").glob("*.json")
            for message in self._load("outbox/" + file.name)["request"]["params"]["event"]["messages"]
        )


class _OversizedRolloutCase:
    def __init__(self, directory):
        from agent_brain_client.adapters.harnesses.codex import hooks

        self.hooks = hooks
        self.home = Path(directory).resolve()
        self.scope = {"session_id": "synthetic-session", "project_id": "synthetic-project",
                      "cwd": str(self.home)}
        self.turn = "synthetic-turn"
        self.file = self.home / "sessions/2026/01/02/rollout-2026-01-02T03-04-05-synthetic-session.jsonl"
        self.file.parent.mkdir(parents=True)
        self.queue = _RolloutTestQueue(self.home / "queue")
        self.append("session_meta", {"id": self.scope["session_id"]})
        self.append("event_msg", {"type": "task_started", "turn_id": self.turn, "root_turn_id": self.turn})
        self.append("turn_context", {"turn_id": self.turn, "root_turn_id": self.turn, "cwd": self.scope["cwd"]})
        self.hook("UserPromptSubmit", "Synthetic prompt")
        self.message("msg-first", "Synthetic prompt")

    def append(self, kind, payload):
        self.write(json.dumps({"type": kind, "payload": payload}).encode() + b"\n")

    def write(self, data):
        with self.file.open("ab") as stream:
            stream.write(data)

    def message(self, identity, text, *, image=None):
        parts = [{"type": "input_text", "text": text}]
        kinds = ["user.text"]
        if image is not None:
            parts.append({"type": "input_image", "image_url": image})
            kinds.append("user.image")
        self.append("response_item", {
            "type": "message", "role": "user", "id": identity, "content": parts,
            "internal_chat_message_metadata_passthrough": {
                "turn_id": self.turn, "content_item_kinds": kinds,
                "content_item_metadata": [{"provenance": {"type": "user"}} for _ in kinds],
            },
        })

    def exclude(self):
        self.queue._save("excluded.json", True)
        return {"status": "excluded-local-only-or-sensitive", "data_transferred": False}

    def submit(self, payload):
        return self.hooks.stage_hook_messages(
            self.queue, self.scope, self.turn, payload["hook_event_name"],
            payload.get("last_assistant_message") or payload["prompt"], payload,
            native_home=lambda: self.home, exclude=self.exclude,
            excluded=lambda: self.queue._load("excluded.json") is True,
        )

    def hook(self, kind, body):
        payload = {"hook_event_name": kind, "turn_id": self.turn, "cwd": self.scope["cwd"],
                   "session_id": self.scope["session_id"], "transcript_path": str(self.file),
                   "stop_hook_active": False}
        payload["last_assistant_message" if kind == "Stop" else "prompt"] = body
        return self.submit(payload)

    def resume(self):
        self.queue = _RolloutTestQueue(self.home / "queue")
        return self.hooks.resume_completions(self.queue, self.submit, allowed=lambda scope: scope == self.scope)

    def checkpoint(self):
        entries = list((self.queue.cache / "codex-completions").glob("*.json"))
        return self.queue._load("codex-completions/" + entries[0].name) if entries else None


class OversizedCodexRollout(unittest.TestCase):
    def capture(self):
        temp = tempfile.TemporaryDirectory(prefix="synthetic-oversized-rollout-")
        self.addCleanup(temp.cleanup)
        return _OversizedRolloutCase(temp.name)

    def settle(self, case):
        for _ in range(12):
            if not case.resume():
                return
        self.fail("Oversized rollout completion did not settle")

    def test_tool_and_image_rows_advance_the_durable_cursor(self):
        from agent_brain_client.adapters.harnesses.codex import transcript

        case = self.capture()
        body = "x" * (transcript._TAIL_LIMIT + 4096)
        case.append("response_item", {"type": "function_call_output", "output": body})
        case.message("msg-image", "Synthetic image text", image="data:image/png;base64," + body)
        case.message("msg-later", "Synthetic later user")
        self.assertEqual(case.hook("Stop", "Synthetic final")["same_turn_capture"], "bounded-pending")
        before = case.checkpoint()["scan"]
        self.assertTrue(case.resume())
        self.assertGreater(case.checkpoint()["scan"], before + transcript._TAIL_LIMIT)
        self.settle(case)
        self.assertEqual(case.queue.messages(), [
            ("assistant", "Synthetic final"), ("user", "Synthetic image text"),
            ("user", "Synthetic later user"), ("user", "Synthetic prompt"),
        ])
        self.assertFalse(case.resume())
        self.assertEqual(len(case.queue.messages()), 4)

    def test_incomplete_oversized_tail_resumes_after_append(self):
        from agent_brain_client.adapters.harnesses.codex import transcript

        case = self.capture()
        prefix = b'{"type":"response_item","payload":{"type":"function_call_output","output":"'
        case.write(prefix + b"x" * (transcript._TAIL_LIMIT + 4096) + b"\\u12")
        case.hook("Stop", "Synthetic final")
        before = case.checkpoint()["scan"]
        self.assertTrue(case.resume())
        self.assertEqual(case.checkpoint()["scan"], before)
        self.assertEqual(case.queue.messages(), [("user", "Synthetic prompt")])
        case.write(b'34"}}\n')
        case.message("msg-later", "Synthetic later user")
        self.settle(case)
        self.assertEqual(case.queue.messages(), [
            ("assistant", "Synthetic final"), ("user", "Synthetic later user"), ("user", "Synthetic prompt"),
        ])

    def test_oversized_invalid_or_unverified_user_row_retires_the_retry(self):
        from agent_brain_client.adapters.harnesses.codex import transcript

        large = "x" * (transcript._TAIL_LIMIT + 4096)
        for variant, reason in (("invalid", "oversized-rollout-json-invalid"),
                                ("unverified-user", "oversized-rollout-unverified-fields"),
                                ("unverified-text-with-image", "oversized-rollout-unverified-fields")):
            with self.subTest(variant=variant):
                case = self.capture()
                if variant == "invalid":
                    prefix = b'{"type":"response_item","payload":{"type":"function_call_output","output":"'
                    case.write(prefix + large.encode() + b'\\q"}}\n')
                elif variant == "unverified-user":
                    case.message("msg-private", large + "\nLocalOnly: Synthetic private")
                else:
                    case.message(
                        "msg-private", "x" * transcript._TEXT_LIMIT + "\nLocalOnly: Synthetic private",
                        image="data:image/png;base64," + large,
                    )
                case.message("msg-later", "Synthetic later user")
                case.hook("Stop", "Synthetic final")
                self.assertFalse(case.resume())
                self.assertIsNone(case.checkpoint())
                review, = (case.queue.cache / "codex-completions/review").glob("*.json")
                entry = json.loads(review.read_bytes())
                self.assertEqual(entry["diagnostic"]["reason"], reason)
                self.assertEqual(case.queue.messages(), [("user", "Synthetic prompt")])
                result = case.hook("Stop", "Synthetic final")
                self.assertEqual(result["status"], "capture-review-required")
                self.assertFalse(case.resume())

    def test_oversized_image_preserves_local_only_text(self):
        from agent_brain_client.adapters.harnesses.codex import transcript

        case = self.capture()
        case.message("msg-private", "LocalOnly: Synthetic image text",
                     image="data:image/png;base64," + "x" * (transcript._TAIL_LIMIT + 4096))
        case.message("msg-later", "Synthetic later user")
        case.hook("Stop", "Synthetic final")
        self.assertFalse(case.resume())
        self.assertIsNone(case.checkpoint())
        self.assertEqual(case.queue.messages(), [("user", "Synthetic prompt")])
        self.assertTrue(case.queue._load("excluded.json"))


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
            # Foreign-host metadata must not activate its runtime adapter.
            foreign = project_settings(home, project, harness=other)
            foreign.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            metadata.write(foreign, dict(loaded, harness=other))
            foreign_bytes = foreign.read_bytes()
            # Real shared setup path with a nonempty project directory.
            result = host_maintenance.install_sources(SimpleNamespace(device_home=str(home), harness="codebuddy", profile_id=None, operator=None, operator_sha256=None))
            assert result["status"] == "shared-source-runtime-prepared", result
            shared = metadata.private_json(settings_path(home))
            assert shared["legacy_projects"] == [] and shared["enabled"] is False
            assert foreign.read_bytes() == foreign_bytes
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
            value = dict(shared, legacy_projects=[{"root": str(project), "settings": str(output), "settings_sha256": pin}], server_scope_sha256=None)
            calls = []
            class ControlClient:
                def request(self, request):
                    calls.append(request["method"])
                    admitted = request["method"] != "memory_stop"
                    return {"result": {"schema": 2, "lifecycle": "until-disabled", "model_admitted": admitted, "source_admitted": admitted}}
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
                assert calls == ["memory_stop", "memory_resume", "memory_stop", "memory_stop"], calls
                with patch.object(project_memory, "adapter_for", side_effect=OSError("Synthetic remote unavailable")), patch.object(sys, "stdout", io.StringIO()):
                    assert collection.main(["global-resume", "--device-home", str(home)]) == 2
                assert (settings_path(home).parent / "global.off").exists()
            second = home / "synthetic-second-project"
            second.mkdir()
            second_output = project_settings(home, second, harness=harness)
            second_args = SimpleNamespace(**dict(vars(args), project_root=str(second), output=str(second_output)))
            second_setup = project_memory.setup(second_args)
            second_record = project_memory.load(second_output, second_setup["settings_sha256"])
            second_record.update(source_mode="codex-project", capture_policy=str(cap), capture_policy_sha256=cap_pin,
                                 recall_policy=str(recall), recall_policy_sha256=recall_pin)
            second_pin = metadata.write(second_output, second_record)
            second_row = {"root": str(second), "settings": str(second_output), "settings_sha256": second_pin}
            original_row = value["legacy_projects"][0]
            for outcomes in ((False, False), (False, True), (True, False), (True, True), "malformed", "missing", "error", "transport"):
                for reversed_order in (False, True):
                    metadata.write(Path(loaded["off_file"]), {})
                    metadata.write(Path(second_record["off_file"]), {})
                    metadata.write(settings_path(home).parent / "global.off", {})
                    value["legacy_projects"] = [second_row, original_row] if reversed_order else [original_row, second_row]
                    clients = []
                    class AdmissionClient:
                        def __init__(self, settings):
                            self.settings = settings
                        def request(self, request):
                            clients.append(self.settings["project_root"])
                            if self.settings["project_root"] == str(second):
                                return {"result": {"schema": 2, "lifecycle": "until-disabled", "model_admitted": True, "source_admitted": True}}
                            if outcomes == "transport":
                                raise OSError("Synthetic transport failure")
                            if outcomes == "missing":
                                return {}
                            if outcomes == "error":
                                return {"error": {"code": "synthetic-denial"}}
                            if outcomes == "malformed":
                                return {"result": {"schema": 2}}
                            return {"result": {"schema": 2, "lifecycle": "until-disabled", "model_admitted": outcomes[0], "source_admitted": outcomes[1]}}
                    with patch.object(collection.core, "load", return_value=value), patch.object(project_memory, "adapter_for", side_effect=lambda p: (AdmissionClient(p), None)):
                        out = io.StringIO()
                        with patch.object(sys, "stdout", out):
                            rc = collection.main(["global-resume", "--device-home", str(home), "--harness", harness])
                    result = json.loads(out.getvalue())
                    confirmed = outcomes == (True, True)
                    assert rc == (0 if confirmed else 2), result
                    assert result["remote_confirmed"] is confirmed, result
                    assert Path(loaded["off_file"]).exists() is not confirmed
                    assert not Path(second_record["off_file"]).exists()
                    assert (settings_path(home).parent / "global.off").exists() is not confirmed
                    assert set(clients) == {str(project), str(second)} and len(clients) == 2, clients
                    assert all(row["remote_confirmed"] == (confirmed or row["root"] == str(second)) for row in result["projects"]), result
                    assert metadata.private_json(output) == loaded
            # Legacy routing still requires the full identity and code pins.
            from agent_brain_client.adapters.storage.collection_setup import prepare
            metadata.write(output, dict(loaded, profile_sha256="0" * 64))
            metadata.write(settings_path(home), dict(shared, legacy_projects=[]))
            checked = prepare(args, locate=lambda home, runtime, host: content_host(host).location(home, runtime))
            assert all(row["root"] != str(project) for row in checked["legacy_projects"])
            assert len(checked["legacy_projects"]) == (1 if harness == "codex" and os.name != "nt" else 0)
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
