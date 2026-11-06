"""Execute the shipped macOS shell wrapper with a synthetic installed runtime."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from bootstrap.python_runtime import RUNTIMES, dependency

DIGEST = dependency()[1] if sys.platform == "darwin" else RUNTIMES["darwin-arm64"][1]

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "darwin", "Native macOS wrapper")
class MemoryWrapper(unittest.TestCase):
    def test_real_shell_imports_current_bootstrap_and_dispatches_each_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "home"
            home.mkdir()
            for name in (
                "bin/memory.sh",
                "bootstrap/__init__.py",
                "bootstrap/python_runtime.py",
                "src/agent_brain_client/adapters/platforms/filesystem.py",
            ):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, target)
            runtime = home / ".config/agent-brain-client/install-runtime"
            runtime.mkdir(parents=True, mode=0o700)
            prefix = runtime / DIGEST
            python = prefix / "python/bin/python3"
            python.parent.mkdir(parents=True)
            # The real bootstrap verifies this existing interpreter's saved digest.
            # It is a harmless executable trampoline into the test interpreter.
            python.write_text(
                "#!/bin/sh\nexec "
                + "'"
                + sys.executable.replace("'", "'\\''")
                + '\' "$@"\n'
            )
            python.chmod(0o700)
            (prefix / "archive.sha256").write_text(
                json.dumps(
                    {
                        "archive": DIGEST,
                        "python": hashlib.sha256(python.read_bytes()).hexdigest(),
                    }
                )
            )
            entries = {
                "status": "interfaces/cli/project.py",
                "global-stop": "interfaces/cli/collection.py",
                "global-resume": "interfaces/cli/collection.py",
                "content-manage": "interfaces/management/server.py",
            }
            for entry in set(entries.values()):
                path = root / "src/agent_brain_client" / entry
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "import json,sys\nprint(json.dumps({'entry':__file__,'args':sys.argv[1:],'isolated':sys.flags.isolated}))\n"
                )
            for command, entry in entries.items():
                with self.subTest(command=command):
                    result = subprocess.run(
                        [
                            "/bin/sh",
                            str(root / "bin/memory.sh"),
                            command,
                            "synthetic-argument",
                        ],
                        env={
                            "HOME": str(home),
                            "CODEX_HOME": str(home / "codex"),
                            "PATH": "/usr/bin:/bin",
                        },
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    value = json.loads(result.stdout)
                    self.assertEqual(
                        value["entry"], str(root / "src/agent_brain_client" / entry)
                    )
                    self.assertEqual(value["args"], [command, "synthetic-argument"])
                    self.assertEqual(value["isolated"], 1)

    @staticmethod
    def installed_runtime(home, command):
        import shlex

        runtime = home / ".config/agent-brain-client/install-runtime"
        runtime.mkdir(parents=True, mode=0o700)
        prefix = runtime / DIGEST
        python = prefix / "python/bin/python3"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n')
        python.chmod(0o700)
        (prefix / "archive.sha256").write_text(
            json.dumps(
                {
                    "archive": DIGEST,
                    "python": hashlib.sha256(python.read_bytes()).hexdigest(),
                }
            )
        )

    def test_shipped_wrapper_imports_the_actual_four_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            self.installed_runtime(home, [sys.executable])
            for action in (
                "status",
                "global-stop",
                "global-resume",
                "content-manage",
            ):
                with self.subTest(action=action):
                    result = subprocess.run(
                        ["/bin/sh", str(ROOT / "bin/memory.sh"), action, "--help"],
                        env={
                            "HOME": str(home),
                            "CODEX_HOME": str(home / "codex"),
                            "PATH": "/usr/bin:/bin",
                        },
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertIn("usage:", result.stdout)

    def test_real_wrapper_stop_uses_application_fence_before_server_or_project_control(
        self,
    ):
        # Only enrollment/network bindings are fictional. Execute the shipped
        # shell, bootstrap, CLI, runtime wiring, application and native fence I/O.
        fixture = """import json,os,sys,runpy
from pathlib import Path
args=sys.argv[1:]
entry=Path(args[2]);sys.path[:0]=[str(entry.parents[4]/"src"),str(entry.parents[4])]
from agent_brain_client.runtime import collection as core
home=Path(os.environ["HOME"]);mode=os.environ["SYNTHETIC_CONTROL_MODE"]
core.settings_path(home).parent.mkdir(parents=True,exist_ok=True)
value={"device_home":str(home),"server_scope_sha256":"a"*64 if mode.startswith("server") else None,"legacy_projects":[] if mode.startswith("server") else [{"settings":"synthetic","settings_sha256":"b"*64}]}
core.load=lambda *a,**k:value
trace=[]
def record(kind):
 trace.append({"kind":kind,"global_fence":(core.settings_path(home).parent/"global.off").exists(),"project_fence":(home/"project.off").exists()})
 (home/"trace.json").write_text(json.dumps(trace))
class Client:
 def __init__(self,value):pass
 def wire(self,method,params):
  record(method)
  return {"error":{"code":"synthetic-unconfirmed"}} if mode=="server-error" else {"result":{"stopped":True}}
core.CollectionClient=Client
core.project_sources.load=lambda *a:{"off_file":str(home/"project.off")}
core.project_memory.daily_command=lambda source,action:record("project-"+action)
sys.argv=[str(entry),*args[3:]]
runpy.run_path(str(entry),run_name="__main__")
"""
        for mode in ("server-stop", "server-error", "project-stop"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                home = Path(directory).resolve()
                injection = home / "native-bindings.py"
                injection.write_text(fixture)
                self.installed_runtime(
                    home, [sys.executable, "-I", "-B", str(injection)]
                )
                result = subprocess.run(
                    [
                        "/bin/sh",
                        str(ROOT / "bin/memory.sh"),
                        "global-stop",
                        "--device-home",
                        str(home),
                    ],
                    env={
                        "HOME": str(home),
                        "CODEX_HOME": str(home / "codex"),
                        "PATH": "/usr/bin:/bin",
                        "SYNTHETIC_CONTROL_MODE": mode,
                    },
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(
                    result.returncode,
                    2 if mode == "server-error" else 0,
                    result.stdout + result.stderr,
                )
                value = json.loads(result.stdout)
                self.assertEqual(
                    value["status"],
                    "global-memory-review-required"
                    if mode == "server-error"
                    else "global-memory-stopped",
                )
                trace = json.loads((home / "trace.json").read_text())
                self.assertEqual(len(trace), 1)
                self.assertTrue(trace[0]["global_fence"])
                self.assertEqual(trace[0]["project_fence"], mode == "project-stop")
                self.assertEqual(
                    trace[0]["kind"],
                    "project-stop" if mode == "project-stop" else "memory_global_stop",
                )
                self.assertTrue(
                    (
                        home / ".config/agent-brain-client/global-memory/global.off"
                    ).exists()
                )
