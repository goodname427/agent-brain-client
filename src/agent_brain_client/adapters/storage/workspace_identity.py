"""Derive a portable source ID from an enrolled workspace's Git metadata."""

import hashlib
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from agent_brain_client.adapters.storage.origins import discover, resolve_cwd
from agent_brain_client.domain.scopes import within


def remote_identity(remote):
    """Normalize equivalent HTTPS, SSH URL, and Git SCP spellings locally."""
    if not remote or re.search(r"[\s\\]", remote) or re.match(r"^[A-Za-z]:/", remote):
        return None
    if "://" not in remote:
        match = re.fullmatch(r"(?:[a-zA-Z0-9_.-]+@)?([a-zA-Z0-9.-]+):([^:].*)", remote)
        if match is None:
            return None
        remote = "ssh://" + match[1] + "/" + match[2].lstrip("/")
    try:
        parts = urlsplit(remote)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname
    if (
        parts.scheme not in ("https", "ssh") or not host
        or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", host)
        or parts.query or parts.fragment
        or parts.scheme == "ssh" and parts.password is not None
        or port == 0
    ):
        return None
    path = parts.path.lstrip("/").rstrip("/").removesuffix(".git")
    names = path.split("/")
    if len(names) < 2 or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in (".", "..")
        for name in names
    ):
        return None
    if port not in (None, {"https": 443, "ssh": 22}[parts.scheme]):
        return None
    if host == "github.com":
        path = path.lower()
    return host + "/" + path


def _git_executable(root):
    name = "git.exe" if os.name == "nt" else "git"
    for item in os.get_exec_path():
        parent = Path(item)
        if parent.is_absolute():
            candidate = (parent / name).resolve()
            if not candidate.is_relative_to(root) and candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def onboarding_workspace_project(cwd, route):
    """Read the known workspace only when it belongs to the approved route."""
    if cwd is None or route["kind"] != "project" or not Path(cwd).is_absolute():
        return None
    try:
        root = route["project_root"]
        if not within(cwd, root) or not within(os.fspath(resolve_cwd(cwd, strict=True)), root):
            return None
        local = discover(cwd)
        if local["project_root"] != root or local["repository_root"] != route["repository_root"]:
            return None
        return workspace_project(local)
    except (OSError, RuntimeError, ValueError):
        return None


def workspace_project(local):
    """Identify the observed workspace, if its repository declares one remote."""
    if not local or local.get("kind") != "project":
        return None
    try:
        root = resolve_cwd(local["project_root"], strict=True)
        marker = root / ".git"
        if marker.is_symlink() or not (marker.is_dir() or marker.is_file()):
            return None
        git = _git_executable(root)
        if git is None:
            return None
        # Local config resolves a linked worktree's common Git directory.
        # Ignore environment overrides, includes, and URL rewrite rules.
        env = {key: val for key, val in os.environ.items() if not key.upper().startswith("GIT_")}
        data = subprocess.run(
            [str(git), "-C", os.fspath(root), "config", "--local", "--no-includes",
             "--null", "--get-regexp", r"^remote\..+\.url$"],
            cwd=git.parent, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=1, check=True,
        ).stdout
        if len(data) > 8192 or not data.endswith(b"\0"):
            return None
        entries = [row.split("\n", 1) for row in data[:-1].decode("utf-8").split("\0")]
        remotes = [remote for key, remote in entries if key == "remote.origin.url"]
        if not remotes:
            remotes = [remote for _, remote in entries]
        ids = [remote_identity(remote) for remote in remotes]
        if not ids[0] or any(identity != ids[0] for identity in ids[1:]):
            return None
        repository = ids[0]
        if len(repository) > 512:
            return None
        return {"id": "git-sha256:" + hashlib.sha256(repository.encode()).hexdigest(),
                "repository": repository}
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return None
