"""Resolve host-selected workspace provenance independently of source routing."""

from dataclasses import dataclass
from functools import cached_property
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unicodedata
from urllib.parse import urlsplit
from xml.etree import ElementTree

from agent_brain_client.adapters.storage.origins import resolve_cwd
from agent_brain_client.domain.events import validate_workspace
from agent_brain_client.domain.scopes import digest, ordinary_drive_path


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


def _executable(name, root):
    binary = name + (".exe" if os.name == "nt" else "")
    parents = [Path(entry) for entry in os.get_exec_path() if Path(entry).is_absolute()]
    if os.name == "nt" and name == "git":
        # The desktop and detached worker need not inherit the interactive PATH.
        parents.extend(
            Path(os.environ[key]) / suffix
            for key, suffix in (
                ("ProgramFiles", "Git/cmd"),
                ("ProgramFiles(x86)", "Git/cmd"),
                ("LOCALAPPDATA", "Programs/Git/cmd"),
            )
            if os.environ.get(key) and Path(os.environ[key]).is_absolute()
        )
    for parent in parents:
        candidate = (parent / binary).resolve()
        if not candidate.is_relative_to(root) and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _metadata(command, *, cwd, env=None, limit=16384):
    result = subprocess.run(
        list(map(os.fspath, command)), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=1, check=True,
    ).stdout
    if len(result) > limit:
        raise ValueError("Workspace metadata exceeds bound")
    return result


def _git_project(root):
    git = _executable("git", root)
    if git is None:
        return None
    # Local config resolves a linked worktree's common Git directory. Ignore
    # environment overrides, includes and URL rewrites.
    env = {key: val for key, val in os.environ.items() if not key.upper().startswith("GIT_")}
    data = _metadata(
        [git, "-C", root, "config", "--local", "--no-includes", "--null",
         "--get-regexp", r"^remote\..+\.url$"], cwd=git.parent, env=env, limit=8192,
    )
    if not data.endswith(b"\0"):
        return None
    entries = [row.split("\n", 1) for row in data[:-1].decode("utf-8").split("\0")]
    remotes = [remote for key, remote in entries if key == "remote.origin.url"]
    if not remotes:
        remotes = [remote for _, remote in entries]
    identities = {remote_identity(remote) for remote in remotes}
    if len(identities) != 1 or None in identities:
        return None
    repository, = identities
    if len(repository) > 512:
        return None
    return {"id": "git-sha256:" + hashlib.sha256(repository.encode()).hexdigest(), "repository": repository}


def _svn_identity(root):
    svn = _executable("svn", root)
    if svn is None:
        return None
    # A local target without operative or peg revisions uses only WC metadata.
    # Explicit revisions (even BASE) can enter Subversion's repository path.
    with tempfile.TemporaryDirectory(prefix="workspace-svn-") as config:
        raw = _metadata(
            [svn, "info", "--xml", "--non-interactive", "--no-auth-cache", "--config-dir", config,
             "--depth", "empty", "--", str(root) + "@"], cwd=svn.parent,
        )
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        return None
    entries = ElementTree.fromstring(raw).findall("entry")
    if len(entries) != 1:
        return None
    entry = entries[0]
    if entry.get("kind") != "dir" or resolve_cwd(entry.get("path", ""), strict=True) != root:
        return None
    wc_root = Path(entry.findtext("wc-info/wcroot-abspath", ""))
    uuid = entry.findtext("repository/uuid", "").lower()
    relative = entry.findtext("relative-url", "")
    if (
        entry.findtext("wc-info/schedule") != "normal"
        or not wc_root.is_absolute() or not root.is_relative_to(resolve_cwd(wc_root, strict=True))
        or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", uuid)
        or not relative.startswith("^/") or len(relative) > 4096
    ):
        return None
    return "svn-sha256:" + digest([uuid, relative[2:].rstrip("/")])


_P4_ADDRESS = re.compile(r"(?:(ssl|tcp):)?([a-zA-Z0-9]+(?:[.-][a-zA-Z0-9]+)*):([0-9]{1,5})")
_P4_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")
_DEPOT_PATH = re.compile(r"//(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+")


def _p4_address(value):
    match = _P4_ADDRESS.fullmatch(value)
    if match is not None and 0 < int(match[3]) < 65536:
        return f"{match[2].lower()}:{int(match[3])}"
    return None


def _p4_tags(raw):
    fields = {}
    for line in raw.decode("utf-8").splitlines():
        if not line.startswith("... "):
            continue
        name, _, value = line[4:].partition(" ")
        if name in fields:
            raise ValueError("Ambiguous P4 metadata")
        fields[name] = value
    return fields


def _p4_identity(root):
    # Require already configured P4 connection settings from the launching
    # environment. Never take an endpoint, command or password from a checkout.
    port, client = os.environ.get("P4PORT", ""), os.environ.get("P4CLIENT", "")
    if not _p4_address(port) or not _P4_NAME.fullmatch(client):
        return None
    p4 = _executable("p4", root)
    if p4 is None:
        return None
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("P4") or key.upper() in ("P4USER", "P4TICKETS", "P4TRUST", "P4CHARSET")
    }
    # Disable project config/registry redirection. These commands only read
    # an existing spec and server info using the existing connection.
    env.update(P4CONFIG="", P4ENVIRO="")
    command = [p4, "-ztag", "-p", port, "-c", client]
    spec = _p4_tags(_metadata([*command, "client", "-o", client], cwd=p4.parent, env=env))
    if (
        spec.get("Client") != client or not spec.get("Update")
        or any(key.startswith(("AltRoots", "ChangeView")) for key in spec)
        or [key for key in spec if key.startswith("View")] != ["View0"]
    ):
        return None
    client_root = Path(spec.get("Root", ""))
    if not client_root.is_absolute():
        return None
    client_root = resolve_cwd(client_root, strict=True)
    if not root.is_relative_to(client_root):
        return None
    remote, local = shlex.split(spec["View0"])
    stream = spec.get("Stream", "")
    depot = remote.removesuffix("/...")
    if (
        local != "//" + client + "/..." or not remote.endswith("/...")
        or not _DEPOT_PATH.fullmatch(depot) or (stream and not _DEPOT_PATH.fullmatch(stream))
        or any(part in (".", "..") for part in (depot + "/" + stream).split("/"))
    ):
        return None
    info = _p4_tags(_metadata([*command, "info"], cwd=p4.parent, env=env))
    address = _p4_address(info.get("serverAddress", ""))
    server_id = info.get("serverID", "")
    if not address or not _P4_NAME.fullmatch(server_id) or info.get("clientName") != client:
        return None
    mapped = root.relative_to(client_root).as_posix()
    return "p4-sha256:" + digest([address, server_id, stream, depot, mapped])


def _plain_marker(path):
    return not path.is_symlink() and (path.is_dir() or path.is_file())


@dataclass(frozen=True)
class Workspace:
    root: Path

    @cached_property
    def identity(self):
        # Git only describes an exact host-selected root. An outer Git
        # checkout must not collapse two independent leaf workspaces.
        try:
            directories = (self.root, *self.root.parents)
            if _plain_marker(self.root / ".git"):
                project = _git_project(self.root)
                return (project["id"], project) if project is not None else None
            if any(_plain_marker(parent / ".svn") for parent in directories):
                identity = _svn_identity(self.root)
                return (identity, None) if identity is not None else None
            identity = _p4_identity(self.root)
            return (identity, None) if identity is not None else None
        except (OSError, RuntimeError, ValueError, ElementTree.ParseError, subprocess.SubprocessError):
            return None

    @property
    def git_project(self):
        return self.identity[1] if self.identity is not None else None

    def source(self, device):
        if not isinstance(device, str) or not device:
            return None
        name = unicodedata.normalize("NFC", self.root.name).strip()
        value = {"id": "workspace-local-sha256:" + digest([device, os.path.normcase(str(self.root))]),
                 "display_name": name}
        if self.identity is not None:
            value["portable_id"] = self.identity[0]
        try:
            validate_workspace(value)
        except ValueError:
            return None
        return value


def host_workspace(selected_root, event_cwd):
    """Resolve the named host root and prove it contains the observed directory."""
    try:
        if any(not isinstance(p, str) or not Path(p).is_absolute() for p in (selected_root, event_cwd)):
            return None
        root, cwd = (resolve_cwd(ordinary_drive_path(p), strict=True) for p in (selected_root, event_cwd))
        if root.parent == root or not root.is_dir() or not cwd.is_dir() or not cwd.is_relative_to(root):
            return None
        return Workspace(root)
    except (OSError, RuntimeError, ValueError):
        return None


def onboarding_workspace(root, route):
    """Use a root attested by the native original within the existing route."""
    if root is None or not route:
        return None
    # An ordinary directory routes by its approved current directory. The
    # pre-existing project boundary remains the upper bound for metadata.
    boundary = route["project_root"] or route["cwd"]
    if host_workspace(boundary, root) is None:
        return None
    return host_workspace(root, root)


def workspace_project(local):
    """Retain the existing Git-only automatic recall contract."""
    if not local or local.get("kind") != "project":
        return None
    workspace = host_workspace(local["project_root"], local["cwd"])
    if workspace is None or not _plain_marker(workspace.root / ".git"):
        return None
    return workspace.git_project
