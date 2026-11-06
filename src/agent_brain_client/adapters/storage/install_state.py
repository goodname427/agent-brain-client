"""adapters / storage / install_state: concrete installation responsibility."""

from agent_brain_client.adapters.platforms.filesystem import private_directory
from agent_brain_client.adapters.platforms.credentials import owned
import hashlib
import os
from agent_brain_client.adapters.platforms.filesystem import file_mode, locked
from agent_brain_client.adapters.storage.private_files import (
    safe,
    save,
    private_json,
)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def private_lease(state):
    state = safe(state)
    private_directory(state)
    owned(state, directory=True)
    path = safe(state / "first-install.lock")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            file_mode(fd, 0o600)
        finally:
            os.close(fd)
    except FileExistsError:
        pass
    owned(path)
    return locked(path, timeout=0)


def same_private(path, value):
    path = safe(path)
    if path.exists():
        if private_json(path) != value:
            raise ValueError("Prepared configuration drift")
    else:
        save(path, value)
    return sha(path.read_bytes())
