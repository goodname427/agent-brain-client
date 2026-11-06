"""Native regular source file opens with no-follow and link count checks."""

import os
import stat
from pathlib import Path


def canonical(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Linked Client path preserved")
    if os.name == "nt" and any(
        p.exists() and getattr(p.lstat(), "st_file_attributes", 0) & 0x400
        for p in (path, *path.parents)
    ):
        raise ValueError("Reparse Client path preserved")
    return path


def open_native_source(path):
    path = canonical(path)
    handle = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    stream = os.fdopen(handle, "rb")
    info = os.fstat(handle)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        stream.close()
        raise ValueError("Native transcript regular file required")
    return stream, info
