"""Native regular source file opens with no-follow and link count checks."""

import os
import stat
from pathlib import Path


def _io_path(path):
    """Use extended local Win32 paths without requiring machine policy changes."""
    if os.name != "nt":
        return path
    from agent_brain_client.adapters.platforms.windows.source_files import io_path

    return io_path(path)


def canonical(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        checked = _io_path(part)
        if checked.is_symlink():
            raise ValueError("Linked Client path preserved")
        if os.name == "nt" and checked.exists() and getattr(
            checked.lstat(), "st_file_attributes", 0
        ) & 0x400:
            raise ValueError("Reparse Client path preserved")
    return path


def open_native_source(path):
    path = canonical(path)
    handle = os.open(_io_path(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    stream = os.fdopen(handle, "rb")
    info = os.fstat(handle)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        stream.close()
        raise ValueError("Native transcript regular file required")
    return stream, info


def native_source_stat(path):
    return _io_path(canonical(path)).stat()
