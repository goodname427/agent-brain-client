"""Fast blocking checks; this command never produces release evidence."""

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from dev.checks import scope

TARGETS = [
    (
        "src/agent_brain_client/interfaces/",
        ["test_host_entrypoints", "test_collection_control"],
    ),
    ("src/agent_brain_client/adapters/harnesses/", ["test_host_entrypoints"]),
    ("dev/checks/", ["test_client_checks"]),
    ("dev/release/", ["test_release_pipeline"]),
    ("dev/build/", ["test_client_export"]),
    ("src/agent_brain_client/adapters/packages/", ["test_client_export"]),
    ("src/agent_brain_client/application/", ["test_application_workflows"]),
    (
        "src/agent_brain_client/adapters/platforms/windows/",
        ["test_install_contracts.Contracts"],
    ),
]


def selected_tests(paths):
    selected = {"test_architecture"}
    for path in paths:
        for prefix, modules in TARGETS:
            if path.startswith(prefix) or path in {
                "tests/" + m.split(".")[0] + ".py" for m in modules
            }:
                selected.update(modules)
    return sorted(selected)


def main():
    # Fail closed when the event diff cannot be resolved. No empty-list discovery.
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    name = os.environ["GITHUB_EVENT_NAME"]
    base, head = scope.event_range(name, event)
    paths = [
        raw.decode("utf-8")
        for raw in scope.git(
            "diff", "--name-only", "--no-renames", "-z", base, head, "--"
        ).split(b"\0")
        if raw
    ]
    # Parse every tracked Python file, including tests, without importing code.
    for raw in scope.git("ls-files", "-z").split(b"\0"):
        if raw.endswith(b".py"):
            path = ROOT / os.fsdecode(raw)
            if path.is_symlink():
                raise ValueError(
                    "Python source symlink requires review: " + os.fsdecode(raw)
                )
            ast.parse(path.read_bytes(), filename=os.fsdecode(raw))
    scope.git("diff", "--check", base, head, "--")
    tests = selected_tests(paths)
    print(
        json.dumps({"tier": "quick", "tests": tests, "full_validation": False}),
        flush=True,
    )
    with tempfile.TemporaryDirectory(prefix="brain-quick-") as tmp:
        home, codex = Path(tmp) / "home", Path(tmp) / "codex"
        home.mkdir()
        codex.mkdir()
        env = dict(
            os.environ,
            HOME=str(home),
            USERPROFILE=str(home),
            CODEX_HOME=str(codex),
            PYTHONPATH=os.pathsep.join(str(ROOT / p) for p in ("src", ".", "tests")),
            PYTHONDONTWRITEBYTECODE="1",
        )
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "PYTHONHOME", "PYTHONSTARTUP"):
            env.pop(key, None)
        for test in tests:
            subprocess.run(
                [sys.executable, "-B", "-m", "unittest", test],
                cwd=ROOT,
                env=env,
                check=True,
                timeout=180,
            )
        for script in ("dev/build/package.py", "dev/checks/source.py"):
            subprocess.run(
                [
                    sys.executable,
                    "-B",
                    script,
                    *(["--scan"] if "package" in script else []),
                ],
                cwd=ROOT,
                env=env,
                check=True,
                timeout=60,
            )


if __name__ == "__main__":
    main()
