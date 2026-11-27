"""Isolated synthetic AB native registration probe. No real host data or Server."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import io
import json
import os
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
from urllib.request import urlopen

from agent_brain_client.adapters.harnesses.codebuddy import configuration as buddy
from agent_brain_client.adapters.harnesses.codex import collection_installation as codex
from agent_brain_client.adapters.packages import build
from agent_brain_client.adapters.platforms.filesystem import native_platform, private_directory
from agent_brain_client.adapters.storage.project_settings import project_settings
from agent_brain_client.runtime import project_memory
from test_codebuddy_output_ack import _PIPE_FILL
from test_stable_native_hooks import ROOT, StableHooks, hook_launcher

DRIVER = """
import threading
import time
""" + _PIPE_FILL + """
_original_run_native_hook = run_native_hook
def run_native_hook(home, harness, settings=None):
    from agent_brain_client.adapters.harnesses import hook_output
    from agent_brain_client.adapters.harnesses.codebuddy import events, transcript
    from agent_brain_client.application import control
    from agent_brain_client.runtime import collection as core, project_memory as project, delivery

    mode, version, real_deadline = json.loads((Path(home) / "synthetic-mode.json").read_text())
    (Path(home) / "synthetic-entered").touch()
    if mode == "recall":
        real_control = control.RequestControl
        control.RequestControl = lambda seconds: real_control(min(seconds, 1.25))
    if not real_deadline and mode in ("stdout", "stderr", "prepare") and hasattr(hook_output, "native_output_deadline"):
        actual_deadline = hook_output.native_output_deadline
        hook_output.native_output_deadline = lambda seconds: actual_deadline(min(seconds, 2))

    def marker(stage):
        (Path(home) / ("synthetic-" + stage)).touch()

    def stage(*args, **kwargs):
        marker("stage")
        return {"status": "staged"}

    def schedule(*args, **kwargs):
        marker("schedule")
        if mode == "prepare":
            time.sleep(130 if real_deadline and version != "0.8.49" else 4)

    def recall(*args, **kwargs):
        marker("recall")
        if mode in ("recall", "near"):
            stop = time.monotonic() + (28.15 if mode == "near" else 3)
            while time.monotonic() < stop:
                control.check_request()
                time.sleep(.04)
        if mode in ("stdout", "stderr"):
            descriptor = sys.stdout.fileno() if mode == "stdout" else sys.stderr.fileno()
            fill_unread_pipe(descriptor)
            marker("pipe-full")
        return [{"content": "Synthetic recall marker", "layer": "L1"}]

    root = os.getcwd()
    local = {"kind": "non-project", "cwd": root, "project_root": None, "repository_root": None}
    scope = {"schema": 1, "profile_id": "a" * 32, "include_roots": [],
             "exclude_roots": [], "non_project_roots": [root], "recall_personal": True}
    core.load = lambda *args, **kwargs: {"device_home": home, "scope": scope}
    core.approved = lambda value: True
    core.eligible = lambda *args, **kwargs: local
    core.legacy = lambda *args: None
    core.submit_hook = stage
    events.capture_messages = stage
    transcript.observe = lambda *args, **kwargs: "unavailable"
    core.schedule_delivery = schedule
    core.recall = recall
    project.recalled = recall
    project.capture = stage
    project.capture_enabled = lambda p: False
    if mode == "prepare" and settings is not None:
        original_load = project.load
        def load(*args, **kwargs):
            value = original_load(*args, **kwargs)
            return dict(value, capture_policy="synthetic")
        project.load = load
    delivery.schedule_project = schedule
    return _original_run_native_hook(home, harness, settings)
"""


def register(f, harness, scope):
    if scope == "project":
        workspace = private_directory(f.home / (harness + "-project"))
        path = project_settings(f.home, workspace, harness=harness)
        args = SimpleNamespace(device_home=f.home, project_root=workspace, output=path,
                               harness=harness, query="synthetic", server_bundle=None)
        pin = project_memory.setup(args)["settings_sha256"]
        settings = project_memory.load(path, pin)
        project_memory.install(settings, path, pin)
        target = workspace / (".codex/hooks.json" if harness == "codex" else ".codebuddy/settings.local.json")
        installed = json.loads(target.read_bytes())
    elif harness == "codex":
        workspace = f.home
        f.config.write_text("[features]\nhooks = true\n", encoding="utf-8")
        codex.transact(f.native, f.hook_dir, codex.command_block(hook_launcher.native_hook_command(f.home, harness)))
        installed = tomllib.loads(f.config.read_text(encoding="utf-8"))
    else:
        workspace = f.home
        _, hooks = buddy.definitions(f.state / "synthetic-entry", f.home, fixed_hooks=True)
        target = private_directory(f.home / ".codebuddy") / "settings.json"
        target.write_bytes(buddy.render_json("{}", hooks, None, hooks=True))
        installed = json.loads(target.read_bytes())
    row = installed["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    command = row.get("commandWindows", row["command"]) if os.name == "nt" else row["command"]
    return workspace, command, row["timeout"]


def run(f, command, workspace, harness, scope, mode, version, host_budget):
    env = dict(os.environ, HOME=str(f.home), USERPROFILE=str(f.home), CODEX_HOME=str(f.native),
               CODEBUDDY_PROJECT_DIR=str(workspace), AGENT_BRAIN_RECALL_TIMING="1",
               APPDATA=str(f.home / "AppData/Roaming"), LOCALAPPDATA=str(f.home / "AppData/Local"))
    if harness == "codex" and os.name == "nt":
        argv = subprocess.list2cmdline([os.environ["COMSPEC"]]) + ' /C "' + command + '"'
        assert len(argv.encode("utf-16le")) // 2 <= 8191
    else:
        argv = command if os.name == "nt" else shlex.split(command)
    real_deadline = os.environ.get("AB_MATRIX_REAL_DEADLINE") == "1"
    (f.home / "synthetic-mode.json").write_text(json.dumps([mode, version, real_deadline]), encoding="utf-8")
    for key in ("entered", "stage", "schedule", "recall", "pipe-full"):
        (f.home / ("synthetic-" + key)).unlink(missing_ok=True)
    payload = json.dumps({"hook_event_name":"UserPromptSubmit","cwd":str(workspace),"session_id":"synthetic",
                          "generation_id":mode,"turn_id":mode,"prompt":"Synthetic question"}).encode()
    start = time.monotonic()
    if mode in ("stdout","stderr"):
        with subprocess.Popen(argv,cwd=workspace,env=env,stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              start_new_session=os.name != 'nt') as child:
            try:
                child.stdin.write(payload)
                child.stdin.close()
                child.stdin = None
                status = child.wait(timeout=132 if real_deadline and version != "0.8.49" else 6)
                wall = time.monotonic() - start
            except subprocess.TimeoutExpired:
                status = None
                wall = time.monotonic() - start
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(child.pid)], capture_output=True)
                else:
                    os.killpg(child.pid, signal.SIGKILL)
            stdout, stderr = child.communicate()
    else:
        result = subprocess.run(argv, cwd=workspace, env=env, input=payload, capture_output=True, timeout=132 if real_deadline else 45)
        stdout,stderr,status = result.stdout,result.stderr,result.returncode
        wall = time.monotonic() - start
    marker = lambda name: (f.home/("synthetic-"+name)).exists()
    item = {"os":sys.platform,"version":version,"host":harness,"scope":scope,"scenario":mode,
            "registered_host_budget_s":host_budget,"real_deadline":real_deadline,"process_ms":round(wall*1000,1),"exit":status,
            "context":b"Synthetic recall marker" in stdout,"recall_called":marker("recall"),
            "background_called":marker("schedule"),"pipe_filled":marker("pipe-full"),
            "watchdog":b"native-hook-deadline" in stderr, "entered":marker("entered"),
            "stdout_bytes":len(stdout), "stderr_bytes":len(stderr),
            "startup_error":b"Hook launch unavailable" in stderr or b"bootstrap verification failed" in stderr}
    print("AB_ENTRY_PROBE "+json.dumps(item),flush=True)
    if mode in ("normal", "near"):
        assert status==0 and item["context"], item
        if mode == "near":
            assert item["process_ms"] < 30000, item
            assert json.loads(stdout)["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit", item
    elif mode == "recall":
        assert item["recall_called"] and status==0, item
        assert item["context"] == (version=="0.8.49" and harness=="codebuddy" and scope=="global"), item
    elif mode == "prepare":
        assert item["background_called"] and status == 0, item
        assert item["recall_called"] == (version == "0.8.49"), item
    else:
        assert marker("pipe-full") and status==(None if version=="0.8.49" else 0), item
        if real_deadline and harness == "codebuddy" and version == "0.8.50":
            assert item["process_ms"] < 30000, item


def fixture(version):
    f=StableHooks()
    f.setUp()
    try:
        if version=="0.8.49":
            archive=f.root/"old.tar"
            baseline = "b87b0bc44ce29ede1f71e26d8f6ab9fd3453ba2e"
            base = f.root / "old"
            base.mkdir()
            local = subprocess.run(["git", "archive", "--output", str(archive), baseline], cwd=ROOT, capture_output=True)
            if local.returncode:
                # The official Windows job checks out only its own commit.
                url = "https://codeload.github.com/goodname427/agent-brain-client/tar.gz/" + baseline
                with urlopen(url, timeout=20) as response:
                    content = response.read(10_000_001)
                assert len(content) <= 10_000_000
                archive.write_bytes(content)
                with tarfile.open(archive) as files:
                    files.extractall(base, filter="data")
                base = base / ("agent-brain-client-" + baseline)
            else:
                with tarfile.open(archive) as files:
                    files.extractall(base, filter="data")
            f.bundle=f.root/"old-bundle"
            with patch.object(build,"ROOT",base):
                f.files=build.export(base,f.bundle,https_content_candidate=True,target_platform=native_platform())["files"]
        f.release(version,("2" if version=="0.8.49" else "3")*40,instrument=False,extra=DRIVER.encode())
        filters = os.environ.get("AB_MATRIX_FILTER", "").split(",")
        for harness in ("codex","codebuddy"):
            if any(host in filters for host in ("codex", "codebuddy")) and harness not in filters:
                continue
            for scope in ("global","project"):
                workspace,command,host_budget=register(f,harness,scope)
                if any(kind in filters for kind in ("global", "project")) and scope not in filters:
                    continue
                for mode in ("normal","recall","prepare","stdout","stderr","near"):
                    if mode == "near" and mode not in filters:
                        continue
                    if any(fault in filters for fault in ("normal","recall","prepare","stdout","stderr","near")) and mode not in filters:
                        continue
                    run(f,command,workspace,harness,scope,mode,version,host_budget)
    finally:
        f.doCleanups()


if __name__ == "__main__":
    for version in ("0.8.49","0.8.50"):
        filters = os.environ.get("AB_MATRIX_FILTER", "").split(",")
        if not any(candidate in filters for candidate in ("0.8.49","0.8.50")) or version in filters:
            fixture(version)
