"""Directory-relative no-follow source access on POSIX."""

import os
import stat
from pathlib import Path
from agent_brain_client.adapters.platforms.tree import VerifiedTreeFiles, same_object


class PosixTree(VerifiedTreeFiles):
    """Directory-relative no-follow I/O; directory references are owned fds."""

    def __init__(self, root, *, pin_root=False):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        if not pin_root:
            self.root = os.open(root, flags)
            return
        path = Path(root).absolute()
        if ".." in path.parts:
            raise OSError("Native source path must be unambiguous")
        fd = os.open(path.anchor, flags)
        try:
            for part in path.parts[1:]:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
        except BaseException:
            os.close(fd)
            raise
        self.root = fd

    def scan(self, directory):
        return os.scandir(directory)

    def info(self, directory, entry):
        return entry.stat(follow_symlinks=False)

    def info_name(self, directory, name):
        return os.stat(name, dir_fd=directory, follow_symlinks=False)

    def open_directory(self, directory, entry, info):
        fd = os.open(
            entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
        )
        try:
            opened = os.fstat(fd)
            if not stat.S_ISDIR(opened.st_mode) or not same_object(info, opened):
                raise OSError()
            return fd
        except BaseException:
            os.close(fd)
            raise

    def open_file(self, directory, entry, info):
        return os.open(
            entry.name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory,
        )

    def close_directory(self, directory):
        os.close(directory)

    release_directory = close_directory

    def close(self):
        pass
