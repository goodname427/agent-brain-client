"""Discover only current directory and Git marker metadata, never history."""

import os
from pathlib import Path


def discover(cwd=None):
    current = Path(cwd or os.getcwd()).resolve(strict=True)
    if not current.is_dir():
        raise ValueError("Local working directory required")
    for root in (current, *current.parents):
        marker = root / ".git"
        if marker.is_dir():
            return {
                "kind": "project",
                "cwd": str(current),
                "project_root": str(root),
                "repository_root": str(root),
            }
        if marker.is_file():
            if marker.stat().st_size > 4096 or marker.is_symlink():
                raise ValueError("Unrecognized worktree metadata")
            line = marker.read_text(encoding="utf-8").strip()
            if not line.startswith("gitdir: "):
                raise ValueError("Unrecognized worktree metadata")
            gitdir = Path(line[8:])
            gitdir = (
                (root / gitdir).resolve()
                if not gitdir.is_absolute()
                else gitdir.resolve()
            )
            # Standard worktree layout; no Git config, remote, history or body IO.
            common = gitdir.parent.parent
            repository = (
                common.parent
                if gitdir.parent.name == "worktrees" and common.name == ".git"
                else root
            )
            return {
                "kind": "project",
                "cwd": str(current),
                "project_root": str(root),
                "repository_root": str(repository),
            }
    return {
        "kind": "non-project",
        "cwd": str(current),
        "project_root": None,
        "repository_root": None,
    }
