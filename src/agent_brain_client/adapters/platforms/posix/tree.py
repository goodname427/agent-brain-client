"""Directory-relative no-follow source access on POSIX."""

import os
import stat
from agent_brain_client.adapters.platforms.tree import VerifiedTreeFiles, same_object


class PosixTree(VerifiedTreeFiles):
    """Directory-relative no-follow I/O; directory references are owned fds."""

    def __init__(self, root):
        self.root = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def scan(self, directory):
        return os.scandir(directory)

    def info(self, directory, entry):
        return entry.stat(follow_symlinks=False)

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

    def close(self):
        pass
