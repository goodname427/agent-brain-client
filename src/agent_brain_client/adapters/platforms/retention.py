"""No-follow, same-volume retention moves; never remove a retained item."""

import os
from pathlib import Path
from agent_brain_client.adapters.platforms.filesystem import (
    private_mode,
    sync_directory,
)


class RetentionFailure(OSError):
    """Keep the original OS evidence without inferring a lock or ACL cause."""

    def __init__(self, error, operation, source, destination):
        super().__init__(error.errno, error.strerror, error.filename)
        self.details = {
            "operation": operation, "source": str(source),
            "destination": str(destination), "failed_path": str(error.filename) if error.filename else None,
            "failed_path2": str(error.filename2) if error.filename2 else None,
            "error_type": type(error).__name__, "errno": error.errno,
            "winerror": getattr(error, "winerror", None), "os_message": error.strerror,
            "cause": "unknown",
        }


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
    operation = "validate-paths"
    try:
        source, destination = canonical(source), canonical(destination)
        operation = "validate-destination-parent"
        private_mode(destination.parent, directory=True, owner=True)
        operation = "validate-item-identity"
        if destination.exists():
            if source.exists() or item_identity(destination) != identity:
                raise ValueError("Retention destination changed")
            return
        if item_identity(source) != identity:
            raise ValueError("Retention source changed")
        operation = "rename"
        os.rename(source, destination)
        operation = "sync-source-parent"
        sync_directory(source.parent)
        operation = "sync-destination-parent"
        sync_directory(destination.parent)
    except OSError as error:
        raise RetentionFailure(error, operation, source, destination) from error


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
