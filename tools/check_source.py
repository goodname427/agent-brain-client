"""Compile every repository Python source without bytecode or external imports."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check():
    count = 0
    for path in sorted(ROOT.rglob("*.py")):
        if any(
            part in (".git", ".local", "__pycache__", ".mypy_cache", ".ruff_cache")
            for part in path.relative_to(ROOT).parts
        ):
            continue
        if path.is_symlink():
            raise ValueError("Linked public source")
        compile(path.read_bytes(), path.relative_to(ROOT).as_posix(), "exec")
        count += 1
    return {
        "status": "source-syntax-checked",
        "python_files": count,
        "bytecode_written": False,
    }


if __name__ == "__main__":
    print(json.dumps(check()))
