"""Read-only source trees with object identity and version guarantees.

Harnesses choose sources and read bounds. Platforms own native handles, no-follow
opens and checks that the file read is the same object before and after reading.
"""

from contextlib import contextmanager
import os
import stat


def is_link(info):
    # Junctions, mount points and cloud placeholders need not be S_IFLNK.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def same_object(before, after):
    return (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


def file_version(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class VerifiedTreeFiles:
    @contextmanager
    def verified_file(self, directory, entry, info):
        fd = self.open_file(directory, entry, info)
        with os.fdopen(fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or is_link(opened)
                or not same_object(info, opened)
            ):
                raise OSError("Source object changed")
            yield source
            if file_version(opened) != file_version(os.fstat(source.fileno())):
                raise OSError("Source changed during read")


def open_tree(root):
    if os.name == "nt":
        from agent_brain_client.adapters.platforms.windows.tree import WindowsTree

        return WindowsTree(root)
    from agent_brain_client.adapters.platforms.posix.tree import PosixTree

    return PosixTree(root)
