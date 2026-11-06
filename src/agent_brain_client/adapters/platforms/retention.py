"""No-follow, same-volume retention moves; never remove a retained item."""

import os
from pathlib import Path
from agent_brain_client.adapters.platforms.filesystem import (
    private_mode,
    sync_directory,
)


def canonical(path, *, existing=False):
    path = Path(path)
    if os.name == "nt":
        from agent_brain_client.adapters.platforms.windows.native import (
            canonical as windows_canonical,
        )

        windows_canonical(path)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or any(p.is_symlink() for p in (path, *path.parents))
        or existing
        and not path.exists()
    ):
        raise ValueError("Canonical retention path required")
    return path


def item_identity(path):
    path = canonical(path, existing=True)
    info = path.stat()
    if not path.is_dir() and (not path.is_file() or info.st_nlink != 1):
        raise ValueError("Retain only ordinary filesystem items")
    return [info.st_dev, info.st_ino, path.is_dir()]


def retain_item(source, destination, identity):
    source, destination = canonical(source), canonical(destination)
    private_mode(destination.parent, directory=True, owner=True)
    if destination.exists():
        if source.exists() or item_identity(destination) != identity:
            raise ValueError("Retention destination changed")
        return
    if item_identity(source) != identity:
        raise ValueError("Retention source changed")
    os.rename(source, destination)
    sync_directory(source.parent)
    sync_directory(destination.parent)


def retention_root(path):
    path = canonical(path, existing=True)
    if os.name == "nt":
        from agent_brain_client.adapters.platforms.filesystem import cache_mode

        cache_mode(path, directory=True, owner=True)
    else:
        info = path.stat()
        if not path.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("Owned non-writable retention root required")
    return path
