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
from bootstrap.python_runtime import DIGEST

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
                "project-memory": "interfaces/cli/project.py",
                "global-stop": "runtime/collection.py",
                "global-resume": "runtime/collection.py",
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
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    value = json.loads(result.stdout)
                    self.assertEqual(
                        value["entry"], str(root / "src/agent_brain_client" / entry)
                    )
                    self.assertEqual(value["args"], [command, "synthetic-argument"])
                    self.assertEqual(value["isolated"], 1)
