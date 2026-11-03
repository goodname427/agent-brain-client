"""Portable lock and explicit Client platform boundaries; stdlib only."""
from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import time
import platform
import sys

MAC_PLATFORMS = ('darwin-arm64', 'darwin-x86_64')
WINDOWS_PLATFORMS = ('win32-amd64', 'win32-arm64')


class LockTimeout(RuntimeError):
    """Bounded wait for a client update lock expired; callers may retain old code."""
    pass


class PlatformUnsupported(ValueError):
    def __init__(self, operation, target):
        self.operation = operation
        self.target = target if target in MAC_PLATFORMS + WINDOWS_PLATFORMS else "unreviewed-platform"
        super().__init__('Platform operation unsupported')

    def result(self, **fields):
        return dict(status='platform-unsupported', operation=self.operation,
                    platform=self.target, **fields)


def windows_machine():
    """Read native architecture without CPython's version-dependent environment.

    IsWow64Process2 reports the host even for an emulated interpreter. Unknown
    architectures/API failures remain unsupported; no environment fallback.
    """
    import ctypes
    from ctypes import wintypes
    if sys.platform != 'win32' or os.name != 'nt':
        raise PlatformUnsupported('platform-detection', 'unreviewed-platform')
    try:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.IsWow64Process2.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT), ctypes.POINTER(wintypes.USHORT)]
        kernel.IsWow64Process2.restype = wintypes.BOOL
        process, native = wintypes.USHORT(), wintypes.USHORT()
        if not kernel.IsWow64Process2(kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)):
            raise OSError('Native architecture unavailable')
        return {0x8664: 'amd64', 0xAA64: 'arm64'}.get(native.value, 'unknown')
    except (AttributeError, OSError):
        raise PlatformUnsupported('platform-detection', 'unreviewed-platform') from None


def native_platform():
    if sys.platform == 'win32' and os.name == 'nt':
        return 'win32-' + windows_machine()
    machine = platform.machine().lower()
    if machine == 'aarch64': machine = 'arm64'
    if sys.platform == 'win32' and machine == 'x86_64': machine = 'amd64'
    return sys.platform + '-' + machine


def platform_paths(target):
    """Syntax only: accepting metadata never authorizes platform operations."""
    if target in MAC_PLATFORMS: return PurePosixPath
    if target in WINDOWS_PLATFORMS: return PureWindowsPath
    raise PlatformUnsupported('configuration', target)


def require_platform(operation, target=None):
    target = target or native_platform()
    supported = MAC_PLATFORMS if sys.platform == 'darwin' and os.name == 'posix' else WINDOWS_PLATFORMS if sys.platform == 'win32' and os.name == 'nt' else ()
    if target not in supported or target != native_platform():
        raise PlatformUnsupported(operation, target)
    return target


def private_mode(path, *, directory=False, owner=False, blocked=ValueError):
    """Check the native access model; mode bits never substitute for a DACL."""
    require_platform('private-metadata')
    if os.name == 'nt':
        from brain_windows import private
        return private(path, directory=directory, owner=owner, blocked=blocked)
    path = Path(path)
    st = path.stat()
    if st.st_mode & 0o077 or not (path.is_dir() if directory else path.is_file()):
        raise blocked('Private filesystem item required')
    if owner and (st.st_uid != os.getuid() or not directory and st.st_nlink != 1):
        raise blocked('Owned private filesystem item required')
    return path


def sync_directory(path):
    require_platform('private-write')
    if os.name == 'nt':
        # Windows has no POSIX directory fsync. File data is flushed before
        # replacement; atomic_replace uses MOVEFILE_WRITE_THROUGH for rename.
        return
    fd = os.open(path, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def private_directory(path):
    require_platform('private-write')
    path = Path(path)
    if os.name != 'nt':
        path.mkdir(parents=True,exist_ok=True,mode=0o700)
        return path
    if not path.exists():
        # Create each missing ancestor separately so children inherit a private
        # DACL before any body is written. Never alter an existing ancestor.
        if not path.parent.exists(): private_directory(path.parent)
        try: path.mkdir(mode=0o700)
        except FileExistsError: pass
        else:
            if os.name == 'nt':
                from brain_windows import protect_new
                protect_new(path, directory=True)
    private_mode(path, directory=True, owner=True)
    return path


def file_mode(fd, mode):
    require_platform('private-write')
    if os.name != 'nt': os.fchmod(fd, mode)
    else:
        from brain_windows import protect_descriptor
        protect_descriptor(fd)


def atomic_replace(source, target):
    require_platform('private-write')
    if os.name != 'nt': return os.replace(source, target)
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    if not api.MoveFileExW(str(source), str(target), 1 | 8):
        raise OSError(ctypes.get_last_error(), 'Atomic private replacement failed')


def command_line(argv):
    if os.name == 'nt':
        # Codex may launch through a Windows shell. Encoded PowerShell keeps
        # percent, dollar, ampersand, quotes and spaces out of shell expansion.
        # Encode Python's -c body separately: Windows PowerShell 5.1 otherwise
        # drops embedded double quotes when forwarding native argv.
        import base64
        import zlib
        argv = list(argv)
        if '-c' in argv:
            i = argv.index('-c') + 1
            payload = base64.b64encode(zlib.compress(argv[i].encode())).decode()
            argv[i] = "exec(__import__('zlib').decompress(__import__('base64').b64decode('" + payload + "')))"
            # Isolated Python ignores PYTHONIOENCODING. Its own UTF-8 switch
            # keeps Hook input/output Unicode-safe on legacy Windows consoles.
            argv[1:1] = ['-X', 'utf8']
        script = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $OutputEncoding=[Console]::OutputEncoding; $env:PYTHONIOENCODING='utf-8'; & " + ' '.join("'" + item.replace("'", "''") + "'" for item in argv) + '; exit $LASTEXITCODE'
        encoded = base64.b64encode(script.encode('utf-16le')).decode()
        return 'powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand ' + encoded
    import shlex
    return shlex.join(argv)


@contextmanager
def locked(path, timeout=20, shared=False, cancel_check=None):
    """Yield the owned handle; POSIX shared leases may be inherited deliberately.

    Existing callers retain exclusive locking by default. Windows workers take
    their own shared lease; Windows byte locks cannot be inherited as ownership.
    """
    path = Path(path)
    require_platform('private-lock')
    if os.name == 'nt':
        from brain_windows import canonical
        canonical(path)
        private_directory(path.parent)
        if path.exists(): private_mode(path, owner=True)
    else: path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        handle = path.open('r+b')
    else:
        try:
            if os.name == 'nt': file_mode(fd, 0o600)
        except BaseException:
            os.close(fd); raise
        handle = os.fdopen(fd, 'r+b')
    if os.name == 'nt': private_mode(path, owner=True)
    acquired = False
    try:
        # msvcrt locks a byte range; a persistent byte makes the lock portable.
        if path.stat().st_size == 0:
            handle.write(b'0')
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            if cancel_check is not None:cancel_check()
            try:
                handle.seek(0)
                if os.name == 'nt':
                    from brain_windows import file_lock
                    file_lock(handle, shared=shared)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout('Another operation is running; retry shortly.')
                time.sleep(.1)
        if cancel_check is not None:cancel_check()
        yield handle
    finally:
        if acquired:
            handle.seek(0)
            if os.name == 'nt':
                from brain_windows import file_lock
                file_lock(handle, release=True)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

