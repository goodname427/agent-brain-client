"""Windows existing host file I/O preserving its object and security descriptor."""

from contextlib import contextmanager
import os


@contextmanager
def open_host_file(path):
    """Open the existing object for data I/O; allow readers, exclude writers.

    OPEN_EXISTING never replaces the object or assigns a security descriptor.
    No DELETE, WRITE_DAC, WRITE_OWNER or ACCESS_SYSTEM_SECURITY is requested.
    """
    import ctypes as C
    from ctypes import wintypes as W
    import msvcrt
    from agent_brain_client.adapters.platforms.windows.native import (
        canonical,
        libraries,
    )

    path = canonical(path)
    _, kernel = libraries()
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
    handle = kernel.CreateFileW(str(path), 0xC0000000, 1, None, 3, 0x00200000, None)
    if handle == W.HANDLE(-1).value:
        raise ValueError("Host configuration is busy or not writable")
    try:
        fd = getattr(msvcrt, "open_osfhandle")(
            handle, os.O_RDWR | getattr(os, "O_BINARY", 0)
        )
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, "r+b", buffering=0) as file:
        st = os.fstat(file.fileno())
        if st.st_nlink != 1 or getattr(st, "st_file_attributes", 0) & 0x400:
            raise ValueError("Single non-reparse host file required")
        yield file
