"""Windows source tree handles, reparse rejection and pinned ancestors."""

import os
from pathlib import Path
from agent_brain_client.adapters.platforms.tree import VerifiedTreeFiles, same_object


class WindowsTree(VerifiedTreeFiles):
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
        # Ordinary source filenames need none of these Win32 aliases. Refuse
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
            raise OSError("File source unavailable or busy")
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
            if not same_object(info, os.stat(path, follow_symlinks=False)):
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
