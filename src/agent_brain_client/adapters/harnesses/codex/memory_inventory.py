"""Codex inventory: concrete native behavior, separate from shared use cases."""

import hashlib
import itertools
import os
from pathlib import Path
import stat
from agent_brain_client.domain import contract
from agent_brain_client.adapters.harnesses.codex.content_limits import sha


def _memory_link(info):
    # Junctions, mount points and cloud placeholders need not be S_IFLNK.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def _memory_same(before, after):
    return (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


def _memory_version(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _PosixMemoryTree:
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
            if not stat.S_ISDIR(opened.st_mode) or not _memory_same(info, opened):
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


class _WindowsMemoryTree:
    """Pin every path component before path-based enumeration on Windows.

    CreateFileW opens the reparse object itself, never its target. Every opened
    object is checked for reparse attributes and disk type. Directory handles
    stay open until the entire walk ends, with only FILE_SHARE_READ: a parent
    cannot be renamed/replaced or changed into a junction while children are
    opened by path. Files use the same sharing mode to exclude writers/deleters.
    No ACL, owner, scope or source content is changed. Busy objects fail closed.
    """

    def __init__(self, root):
        import ctypes as C
        from ctypes import wintypes as W

        self.C = C
        self.W = W
        self.handles = []
        self.kernel = getattr(C, "WinDLL")("kernel32", use_last_error=True)

        class FileInformation(C.Structure):
            _fields_ = [
                ("attributes", W.DWORD),
                ("created", W.FILETIME),
                ("accessed", W.FILETIME),
                ("written", W.FILETIME),
                ("volume", W.DWORD),
                ("size_high", W.DWORD),
                ("size_low", W.DWORD),
                ("links", W.DWORD),
                ("index_high", W.DWORD),
                ("index_low", W.DWORD),
            ]

        self.FileInformation = FileInformation
        self.kernel.CreateFileW.argtypes = [
            W.LPCWSTR,
            W.DWORD,
            W.DWORD,
            C.c_void_p,
            W.DWORD,
            W.DWORD,
            W.HANDLE,
        ]
        self.kernel.CreateFileW.restype = W.HANDLE
        self.kernel.CloseHandle.argtypes = [W.HANDLE]
        self.kernel.CloseHandle.restype = W.BOOL
        self.kernel.GetFileInformationByHandle.argtypes = [
            W.HANDLE,
            C.POINTER(FileInformation),
        ]
        self.kernel.GetFileInformationByHandle.restype = W.BOOL
        self.kernel.GetFileType.argtypes = [W.HANDLE]
        self.kernel.GetFileType.restype = W.DWORD
        root = Path(os.path.abspath(root))
        # Reject Win32 device namespaces and ambiguous DOS names, including ADS.
        if str(root).startswith(("\\\\?\\", "\\\\.\\")):
            raise OSError()
        self.root = root
        try:
            for path in [*reversed(root.parents), root]:
                if path != Path(path.anchor):
                    self._name(path.name)
                self.handles.append(self._open(path, True))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _name(name):
        # Ordinary Codex filenames need none of these Win32 aliases. Refuse
        # them rather than read a stream/device or silently trim a source name.
        if (
            not name
            or name in (".", "..")
            or any(c in name for c in ":\\/")
            or name.endswith((" ", "."))
            or name.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(10)),
                *(f"LPT{i}" for i in range(10)),
            }
        ):
            raise OSError()

    def _open(self, path, directory):
        # GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING,
        # FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS.
        handle = self.kernel.CreateFileW(
            str(path), 0x80000000, 1, None, 3, 0x02200000, None
        )
        if handle == self.W.HANDLE(-1).value:
            raise OSError("Memory source unavailable or busy")
        try:
            info = self.FileInformation()
            if (
                not self.kernel.GetFileInformationByHandle(handle, self.C.byref(info))
                or self.kernel.GetFileType(handle) != 1
                or info.attributes & 0x400
                or bool(info.attributes & 0x10) != directory
            ):
                raise OSError()
            return handle
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise

    def scan(self, directory):
        return os.scandir(directory)

    def info(self, directory, entry):
        self._name(entry.name)
        # DirEntry.stat has zero st_ino/st_dev on Windows 3.11; os.stat has IDs.
        return os.stat(directory / entry.name, follow_symlinks=False)

    def open_directory(self, directory, entry, info):
        path = directory / entry.name
        handle = self._open(path, True)
        try:
            if not _memory_same(info, os.stat(path, follow_symlinks=False)):
                raise OSError()
            self.handles.append(handle)
            return path
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise

    def open_file(self, directory, entry, info):
        import msvcrt

        handle = self._open(directory / entry.name, False)
        try:
            return getattr(msvcrt, "open_osfhandle")(
                handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
            )
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise

    def close_directory(self, directory):
        pass  # Keep all ancestors pinned.

    def close(self):
        for handle in reversed(self.handles):
            self.kernel.CloseHandle(handle)
        self.handles.clear()


def _memory_tree(root):
    return _WindowsMemoryTree(root) if os.name == "nt" else _PosixMemoryTree(root)


def inventory_codex_memories(
    codex_home,
    *,
    max_files=1024,
    max_file_bytes=131072,
    max_total_bytes=16777216,
    max_entries=1024,
    max_depth=16,
):
    """Fingerprint only Codex's persistent memory tree; never inspect sessions.

    The output deliberately contains no source paths or text. Inventoried entries
    remain ``not-checked`` until a separate review classifies and normalizes
    them for import. A digest cursor lets a caller detect a changed inventory
    without treating this scan as coverage or an import plan.
    """
    root = Path(codex_home).expanduser() / "memories"
    rows = []
    total = 0
    walked = 0
    entries_seen = 0
    directories_seen = 0

    def source_id(relative):
        return "codex-memory:" + sha(relative.encode("utf-8", "surrogateescape"))

    if root.is_symlink() or not root.exists():
        return {
            "schema": 1,
            "status": "unavailable",
            "reason": "memory root missing or symlinked",
            "sources": [],
            "cursor": sha(b"codex-memory-root-unavailable"),
        }
    if not root.is_dir():
        return {
            "schema": 1,
            "status": "unavailable",
            "reason": "memory root is not a directory",
            "sources": [],
            "cursor": sha(b"codex-memory-root-invalid"),
        }
    try:
        tree = _memory_tree(root)
    except OSError:
        return {
            "schema": 1,
            "status": "unavailable",
            "reason": "memory root unavailable",
            "sources": [],
            "cursor": sha(b"codex-memory-root-unavailable"),
        }
    pending = [(tree.root, "", 0)]
    try:
        while pending:
            directory_fd, prefix, depth = pending.pop()
            directories_seen += 1
            if directories_seen > max_entries or depth > max_depth:
                rows.append(
                    {
                        "source_id": source_id(prefix or "."),
                        "source_fingerprint": None,
                        "status": "unavailable",
                        "reason": "directory traversal limit reached",
                    }
                )
                tree.close_directory(directory_fd)
                continue
            try:
                with tree.scan(directory_fd) as scan:
                    entries = sorted(
                        itertools.islice(scan, max_entries - entries_seen + 1),
                        key=lambda entry: entry.name,
                        reverse=True,
                    )
            except OSError:
                rows.append(
                    {
                        "source_id": source_id(prefix or "."),
                        "source_fingerprint": None,
                        "status": "unavailable",
                        "reason": "directory unreadable",
                    }
                )
                tree.close_directory(directory_fd)
                continue
            try:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > max_entries:
                        row = {
                            "source_id": source_id(prefix or "."),
                            "source_fingerprint": None,
                            "status": "unavailable",
                            "reason": "entry inventory limit reached",
                        }
                        rows = rows[: max_entries - 1] + [row]
                        return {
                            "schema": 1,
                            "status": "incomplete",
                            "reason": "bounded directory inventory limit reached",
                            "sources": rows,
                            "cursor": sha(contract.encode(rows)),
                        }
                    relative = (prefix + "/" + entry.name).strip("/")
                    try:
                        info = tree.info(directory_fd, entry)
                    except OSError:
                        rows.append(
                            {
                                "source_id": source_id(relative),
                                "source_fingerprint": None,
                                "status": "unavailable",
                                "reason": "entry unreadable",
                            }
                        )
                        continue
                    if _memory_link(info):
                        rows.append(
                            {
                                "source_id": source_id(relative),
                                "source_fingerprint": None,
                                "status": "excluded",
                                "reason": "symlink or reparse source excluded",
                            }
                        )
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        try:
                            child_fd = tree.open_directory(directory_fd, entry, info)
                            pending.append((child_fd, relative, depth + 1))
                        except OSError:
                            rows.append(
                                {
                                    "source_id": source_id(relative),
                                    "source_fingerprint": None,
                                    "status": "unavailable",
                                    "reason": "directory changed or unreadable",
                                }
                            )
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        rows.append(
                            {
                                "source_id": source_id(relative),
                                "source_fingerprint": None,
                                "status": "excluded",
                                "reason": "non-regular source excluded",
                            }
                        )
                        continue
                    walked += 1
                    if walked > max_files:
                        return {
                            "schema": 1,
                            "status": "incomplete",
                            "reason": "file inventory limit reached",
                            "sources": rows,
                            "cursor": sha(contract.encode(rows)),
                        }
                    if (
                        info.st_size > max_file_bytes
                        or total + info.st_size > max_total_bytes
                    ):
                        rows.append(
                            {
                                "source_id": source_id(relative),
                                "source_fingerprint": None,
                                "status": "excluded",
                                "reason": "bounded inventory limit exceeded",
                            }
                        )
                        continue
                    try:
                        file_fd = tree.open_file(directory_fd, entry, info)
                        digest = hashlib.sha256()
                        size = 0
                        with os.fdopen(file_fd, "rb") as source:
                            opened = os.fstat(source.fileno())
                            if (
                                not stat.S_ISREG(opened.st_mode)
                                or _memory_link(opened)
                                or not _memory_same(info, opened)
                            ):
                                raise OSError()
                            while True:
                                chunk = source.read(65536)
                                if not chunk:
                                    break
                                size += len(chunk)
                                total += len(chunk)
                                if size > max_file_bytes or total > max_total_bytes:
                                    raise OverflowError()
                                digest.update(chunk)
                            if _memory_version(opened) != _memory_version(
                                os.fstat(source.fileno())
                            ):
                                raise OSError()
                        rows.append(
                            {
                                "source_id": source_id(relative),
                                "source_fingerprint": digest.hexdigest(),
                                "status": "not-checked",
                                "reason": "fingerprinted; semantic review required",
                            }
                        )
                    except OverflowError:
                        rows.append(
                            {
                                "source_id": source_id(relative),
                                "source_fingerprint": None,
                                "status": "excluded",
                                "reason": "bounded inventory limit exceeded",
                            }
                        )
                    except OSError:
                        rows.append(
                            {
                                "source_id": source_id(relative),
                                "source_fingerprint": None,
                                "status": "unavailable",
                                "reason": "file changed or unreadable",
                            }
                        )
            finally:
                tree.close_directory(directory_fd)
    finally:
        for directory_fd, _, _ in pending:
            try:
                tree.close_directory(directory_fd)
            except OSError:
                pass
        tree.close()
    rows.sort(key=lambda item: item["source_id"])
    snapshot = sha(contract.encode(rows))
    counts: dict[str, int] = {}
    for item in rows:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "schema": 1,
        "status": "complete" if not counts.get("unavailable") else "incomplete",
        "reason": "metadata inventory only; no import coverage claimed",
        "sources": rows,
        "counts": counts,
        "cursor": snapshot,
    }
