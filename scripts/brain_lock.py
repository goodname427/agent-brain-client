"""Portable lock and explicit Client platform boundaries; stdlib only."""
from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import time
import platform
import sys

MAC_PLATFORMS = ('darwin-arm64', 'darwin-x86_64')
WINDOWS_PLATFORMS = ('win32-amd64', 'win32-arm64')


class PlatformUnsupported(ValueError):
    def __init__(self, operation, target):
        self.operation = operation
        self.target = target if target in MAC_PLATFORMS + WINDOWS_PLATFORMS else "unreviewed-platform"
        super().__init__('Platform operation unsupported')

    def result(self, **fields):
        return dict(status='platform-unsupported', operation=self.operation,
                    platform=self.target, **fields)


def native_platform():
    machine = platform.machine().lower()
    return sys.platform + '-' + machine


def platform_paths(target):
    """Syntax only: accepting metadata never authorizes platform operations."""
    if target in MAC_PLATFORMS: return PurePosixPath
    if target in WINDOWS_PLATFORMS: return PureWindowsPath
    raise PlatformUnsupported('configuration', target)


def require_platform(operation, target=None):
    target = target or native_platform()
    if target not in MAC_PLATFORMS or sys.platform != 'darwin':
        raise PlatformUnsupported(operation, target)
    return target


def private_mode(path, *, directory=False, owner=False, blocked=ValueError):
    """Current protected filesystem implementation; Windows ACLs are unimplemented."""
    require_platform('private-metadata')
    path = Path(path)
    st = path.stat()
    if st.st_mode & 0o077 or not (path.is_dir() if directory else path.is_file()):
        raise blocked('Private filesystem item required')
    if owner and (st.st_uid != os.getuid() or not directory and st.st_nlink != 1):
        raise blocked('Owned private filesystem item required')
    return path


def sync_directory(path):
    require_platform('private-write')
    fd = os.open(path, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


@contextmanager
def locked(path, timeout=20, shared=False):
    """Yield the owned handle; POSIX shared leases may be inherited deliberately.

    Existing callers retain exclusive locking by default. Windows locks remain
    exclusive; inherited content-worker leases are a separate POSIX-only path.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    acquired = False
    try:
        # msvcrt locks a byte range; a persistent byte makes the lock portable.
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('Another operation is running; retry shortly.')
                time.sleep(.1)
        yield handle
    finally:
        if acquired:
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

