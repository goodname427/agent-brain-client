"""Repository-only interpreter handoff for the CodeBuddy entry."""

import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]


def main():
    if sys.argv[1:] == ["backend-home"]:
        from agent_brain_client.interfaces.cli.codebuddy import backend_home

        print(backend_home(Path.home()))
        return 0
    from bootstrap.python_runtime import runtime

    python = runtime(Path.home(), prepare=False)
    entry = ROOT / "src/agent_brain_client/interfaces/cli/codebuddy.py"
    os.execv(
        str(python), [str(python), "-X", "utf8", "-I", "-B", str(entry), *sys.argv[1:]]
    )


if __name__ == "__main__":
    raise SystemExit(main())
