"""Windows architecture, file metadata, ACL checks and shell invocation."""

import ctypes as C
from ctypes import wintypes as W
import os
import sys


def machine():
    from agent_brain_client.adapters.platforms.filesystem import PlatformUnsupported

    """Read native architecture without CPython's version-dependent environment.

    IsWow64Process2 reports the host even for an emulated interpreter. Unknown
    architectures/API failures remain unsupported; no environment fallback.
    """
    import ctypes
    from ctypes import wintypes

    if sys.platform != "win32" or os.name != "nt":
        raise PlatformUnsupported("platform-detection", "unreviewed-platform")
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.IsWow64Process2.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.USHORT),
            ctypes.POINTER(wintypes.USHORT),
        ]
        kernel.IsWow64Process2.restype = wintypes.BOOL
        process, native = wintypes.USHORT(), wintypes.USHORT()
        if not kernel.IsWow64Process2(
            kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)
        ):
            raise OSError("Native architecture unavailable")
        return {0x8664: "amd64", 0xAA64: "arm64"}.get(native.value, "unknown")
    except (AttributeError, OSError):
        raise PlatformUnsupported("platform-detection", "unreviewed-platform") from None


class _WindowsFileInformation(C.Structure):
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


class _WindowsSecurityAttributes(C.Structure):
    _fields_ = [("length", W.DWORD), ("descriptor", C.c_void_p), ("inherit", W.BOOL)]


def open_lock(path, *, cache=False):
    """Create with the final owner/DACL, or open an existing item unchanged."""
    import msvcrt
    from agent_brain_client.adapters.platforms.windows.native import libraries, user_sid

    api, kernel = libraries()
    descriptor = C.c_void_p()
    if not cache:
        current = user_sid()
        sddl = (
            "O:"
            + current
            + "D:P"
            + "".join("(A;;FA;;;" + sid + ")" for sid in (current, "SY", "BA"))
        )
        api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            W.LPCWSTR,
            W.DWORD,
            C.POINTER(C.c_void_p),
            C.c_void_p,
        ]
        if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, C.byref(descriptor), None
        ):
            raise ValueError("Private Windows lock security unavailable")
    try:
        security = _WindowsSecurityAttributes(
            C.sizeof(_WindowsSecurityAttributes), descriptor, False
        )
        kernel.CreateFileW.argtypes = [
            W.LPCWSTR,
            W.DWORD,
            W.DWORD,
            C.c_void_p,
            W.DWORD,
            W.DWORD,
            W.HANDLE,
        ]
        kernel.CreateFileW.restype = W.HANDLE
        kernel.CloseHandle.argtypes = [W.HANDLE]
        # OPEN_ALWAYS ignores the descriptor for an existing lock. Do not share
        # deletion: every caller must lock the same persistent filesystem item.
        handle = kernel.CreateFileW(
            str(path),
            0xC0000000,
            3,
            None if cache else C.byref(security),
            4,
            0x00200080,
            None,
        )
        if handle in (None, C.c_void_p(-1).value):
            raise ValueError("Private Windows lock unavailable")
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)


def private_handle(
    handle, *, directory=False, owner=False, blocked=ValueError, check_access=True
):
    from agent_brain_client.adapters.platforms.windows.native import (
        libraries,
        user_sid,
        sid_text,
    )

    api, kernel = libraries()
    kernel.GetFileInformationByHandle.argtypes = [
        W.HANDLE,
        C.POINTER(_WindowsFileInformation),
    ]
    kernel.GetFileInformationByHandle.restype = W.BOOL
    kernel.GetFileType.argtypes = [W.HANDLE]
    kernel.GetFileType.restype = W.DWORD
    descriptor = C.c_void_p()
    owner_sid = C.c_void_p()
    acl = C.c_void_p()
    api.GetSecurityInfo.argtypes = [
        W.HANDLE,
        C.c_int,
        W.DWORD,
        C.POINTER(C.c_void_p),
        C.c_void_p,
        C.POINTER(C.c_void_p),
        C.c_void_p,
        C.POINTER(C.c_void_p),
    ]
    api.GetSecurityInfo.restype = W.DWORD
    api.GetAce.argtypes = [C.c_void_p, W.DWORD, C.POINTER(C.c_void_p)]
    try:
        info = _WindowsFileInformation()
        if (
            not kernel.GetFileInformationByHandle(handle, C.byref(info))
            or kernel.GetFileType(handle) != 1
        ):
            raise blocked("Windows private metadata unavailable")
        if (
            info.attributes & 0x400
            or bool(info.attributes & 0x10) != directory
            or not directory
            and info.links != 1
        ):
            raise blocked("Owned private Windows item required")
        if not check_access:
            return
        if api.GetSecurityInfo(
            handle,
            1,
            5,
            C.byref(owner_sid),
            None,
            C.byref(acl),
            None,
            C.byref(descriptor),
        ):
            raise blocked("Windows DACL could not be checked")
        current = user_sid()
        if not acl or not owner_sid or owner and sid_text(owner_sid) != current:
            raise blocked("Owned private Windows item required")
        # ACL header: revision(1), reserved(1), size(2), AceCount(2).
        count = C.c_ushort.from_address(acl.value + 4).value
        if count > 64:
            raise blocked("Bounded Windows DACL required")
        for i in range(count):
            ace = C.c_void_p()
            if not api.GetAce(acl, i, C.byref(ace)):
                raise blocked("Windows DACL unavailable")
            kind = C.c_ubyte.from_address(ace.value).value
            flags = C.c_ubyte.from_address(ace.value + 1).value
            if flags & 8:
                continue  # INHERIT_ONLY does not grant access to this item.
            if kind == 1:
                continue  # DENY cannot disclose content.
            if kind != 0 or sid_text(ace.value + 8) not in (
                current,
                "S-1-5-18",
                "S-1-5-32-544",
            ):
                raise blocked("Windows item accessible to another principal")
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)


def private_path(
    path, *, directory=False, owner=False, blocked=ValueError, check_access=True
):
    from agent_brain_client.adapters.platforms.windows.native import (
        canonical,
        libraries,
    )

    path = canonical(path)
    _, kernel = libraries()
    # CPython's Windows stat fallback can report nlink=0 from directory data
    # when another caller has the file open. Query full metadata and security
    # through one shared, non-following handle; never accept unknown link counts.
    kernel.CreateFileW.argtypes = [
        W.LPCWSTR,
        W.DWORD,
        W.DWORD,
        C.c_void_p,
        W.DWORD,
        W.DWORD,
        W.HANDLE,
    ]
    kernel.CreateFileW.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    # FILE_READ_ATTRIBUTES | READ_CONTROL; share read/write/delete permits
    # concurrent locks/atomic writes without granting access to another user.
    handle = kernel.CreateFileW(
        str(path),
        0x80 | (0x20000 if check_access else 0),
        7,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    if handle in (None, C.c_void_p(-1).value):
        raise blocked("Windows private metadata unavailable")
    try:
        private_handle(
            handle,
            directory=directory,
            owner=owner,
            blocked=blocked,
            check_access=check_access,
        )
    finally:
        kernel.CloseHandle(handle)
    return path


def file_mode(fd):
    from agent_brain_client.adapters.platforms.windows.native import (
        protect_descriptor,
    )

    try:
        protect_descriptor(fd)
    except ValueError as error:
        # The pinned legacy credential module checks its new file through
        # path stat. Recover only that incomplete-metadata postcheck and
        # repeat the full owner/DACL/link check on the still-owned fd.
        if str(error) != "Owned private Windows item required":
            raise
        import msvcrt

        private_handle(msvcrt.get_osfhandle(fd), owner=True)


def atomic_replace(source, target):
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    if not api.MoveFileExW(str(source), str(target), 1 | 8):
        raise OSError(ctypes.get_last_error(), "Atomic private replacement failed")


def command_line(argv):
    # Codex may launch through a Windows shell. Encoded PowerShell keeps
    # percent, dollar, ampersand, quotes and spaces out of shell expansion.
    # Encode Python's -c body separately: Windows PowerShell 5.1 otherwise
    # drops embedded double quotes when forwarding native argv.
    import base64
    import zlib

    argv = list(argv)
    if "-c" in argv:
        i = argv.index("-c") + 1
        payload = base64.b64encode(zlib.compress(argv[i].encode())).decode()
        argv[i] = (
            "exec(__import__('zlib').decompress(__import__('base64').b64decode('"
            + payload
            + "')))"
        )
        # Isolated Python ignores PYTHONIOENCODING. Its own UTF-8 switch
        # keeps Hook input/output Unicode-safe on legacy Windows consoles.
        argv[1:1] = ["-X", "utf8"]
    script = (
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $OutputEncoding=[Console]::OutputEncoding; $env:PYTHONIOENCODING='utf-8'; & "
        + " ".join("'" + item.replace("'", "''") + "'" for item in argv)
        + "; exit $LASTEXITCODE"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode()
    return (
        "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + encoded
    )
