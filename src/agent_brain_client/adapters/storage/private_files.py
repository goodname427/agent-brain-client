import os
from agent_brain_client.adapters.platforms.filesystem import require_platform

"""Bounded pinned state I/O with compact protocol serialization."""
import json
from pathlib import Path
from agent_brain_client.domain import contract
from agent_brain_client.domain.errors import InstallBlocked
from agent_brain_client.adapters.storage.metadata import unique as pairs
from agent_brain_client.adapters.storage.host_files import atomic
from agent_brain_client.adapters.platforms.filesystem import cache_mode, private_mode
import hashlib


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def safe(path):
    path = Path(path)
    if os.name == "nt":
        from agent_brain_client.adapters.platforms.windows.native import canonical

        canonical(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise InstallBlocked("Linked/noncanonical path")
    return path


def raw(path, limit=262144):
    path = safe(path)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > limit:
        raise InstallBlocked("Unsupported native file")
    return path.read_bytes()


def save(path, value):
    atomic(safe(path), contract.encode(value), 0o600)


def digest_or_none(value):
    return sha(value) if value is not None else None


def private_json(path, *, cache=False):
    require_platform("private-metadata")
    path = safe(path)
    if cache:
        from agent_brain_client.adapters.platforms.filesystem import cache_mode

        cache_mode(path, blocked=InstallBlocked)
    else:
        private_mode(path, blocked=InstallBlocked)
    return json.loads(raw(path, 1048576), object_pairs_hook=pairs)
