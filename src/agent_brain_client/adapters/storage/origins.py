"""Discover local project marker metadata, never history or project contents."""

import os
from pathlib import Path, PureWindowsPath
from agent_brain_client.domain.scopes import ordinary_drive_path, path as source_path


def _plain_file(marker):
    return not marker.is_symlink() and marker.is_file()


def _non_git_project(root):
    # A global P4 config or a launch from HOME must not admit every personal
    # directory. Markers describe a project, not permission to capture it.
    if root == Path.home().resolve() or root.parent == root:
        return False
    p4config = os.environ.get("P4CONFIG", ".p4config")
    # P4CONFIG may be an absolute/global config. Only a local filename is a
    # workspace marker; never traverse or read its potentially secret contents.
    if (
        p4config not in ("", ".", "..")
        and not any(c in p4config for c in ("/", "\\", ":", "\x00"))
        and _plain_file(root / p4config)
    ):
        return True
    # Unreal projects and source-engine workspaces do not require Git (P4 is
    # common). Only inspect marker names/types, with no recursive file scan.
    if any(_plain_file(marker) for marker in root.glob("*.uproject")):
        return True
    engine = root / "Engine"
    build = engine / "Build"
    source = engine / "Source"
    return (
        not engine.is_symlink()
        and not build.is_symlink()
        and not source.is_symlink()
        and source.is_dir()
        and _plain_file(build / "Build.version")
    )


def resolve_cwd(cwd, *, strict=False):
    raw = os.fspath(cwd)
    if os.name == "nt" or raw.startswith("\\\\"):
        drive_path = ordinary_drive_path(raw)
        if not PureWindowsPath(drive_path).drive:
            raise ValueError("Absolute local Windows source required")
        source_path(drive_path)
    return Path(raw).resolve(strict=strict)


def _origin(current, project=None, repository=None):
    # Keep filesystem traversal on the native spelling and emit the existing
    # drive-letter spelling in local identity and wire metadata.
    return {
        "kind": "project" if project is not None else "non-project",
        "cwd": ordinary_drive_path(str(current)),
        "project_root": ordinary_drive_path(str(project)) if project is not None else None,
        "repository_root": ordinary_drive_path(str(repository)) if repository is not None else None,
    }


def discover(cwd=None):
    current = resolve_cwd(cwd or os.getcwd(), strict=True)
    if not current.is_dir():
        raise ValueError("Local working directory required")
    for root in (current, *current.parents):
        marker = root / ".git"
        if marker.is_dir():
            return _origin(current, root, root)
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
            return _origin(current, root, repository)
    # Preserve existing Git/worktree identity even when it also contains a
    # language/engine project marker. Non-Git projects use their local root as
    # repository identity; this does not claim any remote/depot identity.
    for root in (current, *current.parents):
        if _non_git_project(root):
            return _origin(current, root, root)
    return _origin(current)
