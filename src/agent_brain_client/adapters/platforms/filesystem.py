"""Portable lock and explicit Client platform boundaries; stdlib only."""

from contextlib import contextmanager
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import time
import platform
import sys

MAC_PLATFORMS = ("darwin-arm64", "darwin-x86_64")
WINDOWS_PLATFORMS = ("win32-amd64", "win32-arm64")


class LockTimeout(RuntimeError):
    """Bounded wait for a client update lock expired; callers may retain old code."""

    pass


class PlatformUnsupported(ValueError):
    def __init__(self, operation, target):
        self.operation = operation
        self.target = (
            target
            if target in MAC_PLATFORMS + WINDOWS_PLATFORMS
            else "unreviewed-platform"
        )
        super().__init__("Platform operation unsupported")

    def result(self, **fields):
        return dict(
            status="platform-unsupported",
            operation=self.operation,
            platform=self.target,
            **fields,
        )


def native_platform():
    if sys.platform == "win32" and os.name == "nt":
        from agent_brain_client.adapters.platforms.windows.filesystem import machine

        return "win32-" + machine()
    machine = platform.machine().lower()
    if machine == "aarch64":
        machine = "arm64"
    if sys.platform == "win32" and machine == "x86_64":
        machine = "amd64"
    return sys.platform + "-" + machine


def platform_paths(target):
    """Syntax only: accepting metadata never authorizes platform operations."""
    if target in MAC_PLATFORMS:
        return PurePosixPath
    if target in WINDOWS_PLATFORMS:
        return PureWindowsPath
    raise PlatformUnsupported("configuration", target)


def require_platform(operation, target=None):
    target = target or native_platform()
    supported = (
        MAC_PLATFORMS
        if sys.platform == "darwin" and os.name == "posix"
        else WINDOWS_PLATFORMS
        if sys.platform == "win32" and os.name == "nt"
        else ()
    )
    if target not in supported or target != native_platform():
        raise PlatformUnsupported(operation, target)
    return target


def private_mode(path, *, directory=False, owner=False, blocked=ValueError):
    """Check the native access model; mode bits never substitute for a DACL."""
    require_platform("private-metadata")
    if os.name == "nt":
        from agent_brain_client.adapters.platforms.windows.filesystem import (
            private_path,
        )

        return private_path(path, directory=directory, owner=owner, blocked=blocked)
    path = Path(path)
    st = path.stat()
    if st.st_mode & 0o077 or not (path.is_dir() if directory else path.is_file()):
        raise blocked("Private filesystem item required")
    if owner and (st.st_uid != os.getuid() or not directory and st.st_nlink != 1):
        raise blocked("Owned private filesystem item required")
    return path


def cache_mode(path, *, directory=False, owner=False, blocked=ValueError):
    """Validate cache objects without imposing a Windows principal list.

    Windows decides actual read/write access. Inherited enterprise or Harness
    ACEs do not invalidate a cache; existing security descriptors stay intact.
    Credentials, policies and executable packages still use private_mode.
    """
    require_platform("cache-storage")
    if os.name == "nt":
        from agent_brain_client.adapters.platforms.windows.filesystem import (
            private_path,
        )

        return private_path(
            path, directory=directory, blocked=blocked, check_access=False
        )
    return private_mode(path, directory=directory, owner=owner, blocked=blocked)


def cache_directory(path):
    """Use native inherited cache access on Windows; leave existing ACLs intact."""
    if os.name != "nt":
        return private_directory(path)
    require_platform("cache-storage")
    from agent_brain_client.adapters.platforms.windows.native import canonical

    path = canonical(path)
    if not path.exists():
        if not path.parent.exists():
            cache_directory(path.parent)
        try:
            path.mkdir()
        except FileExistsError:
            pass
    cache_mode(path, directory=True)
    return path


def cache_locked(path, **options):
    return locked(path, cache=True, **options)


def sync_directory(path):
    require_platform("private-write")
    if os.name == "nt":
        # Windows has no POSIX directory fsync. File data is flushed before
        # replacement; atomic_replace uses MOVEFILE_WRITE_THROUGH for rename.
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_directory(path):
    require_platform("private-write")
    path = Path(path)
    if os.name != "nt":
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path
    if not path.exists():
        # Create each missing ancestor separately so children inherit a private
        # DACL before any body is written. Never alter an existing ancestor.
        if not path.parent.exists():
            private_directory(path.parent)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            if os.name == "nt":
                from agent_brain_client.adapters.platforms.windows.native import (
                    protect_new,
                )

                protect_new(path, directory=True)
    private_mode(path, directory=True, owner=True)
    return path


def file_mode(fd, mode):
    require_platform("private-write")
    if os.name != "nt":
        os.fchmod(fd, mode)
    else:
        from agent_brain_client.adapters.platforms.windows.filesystem import file_mode

        file_mode(fd)


def atomic_replace(source, target):
    require_platform("private-write")
    if os.name != "nt":
        return os.replace(source, target)
    from agent_brain_client.adapters.platforms.windows.filesystem import atomic_replace

    return atomic_replace(source, target)


def command_line(argv):
    if os.name == "nt":
        from agent_brain_client.adapters.platforms.windows.filesystem import (
            command_line,
        )

        return command_line(argv)
    import shlex

    return shlex.join(argv)


@contextmanager
def locked(path, timeout=20, shared=False, cancel_check=None, *, cache=False):
    """Yield the owned handle; POSIX shared leases may be inherited deliberately.

    Existing callers retain exclusive locking by default. Windows workers take
    their own shared lease; Windows byte locks cannot be inherited as ownership.
    """
    path = Path(path)
    require_platform("private-lock")
    if os.name == "nt":
        from agent_brain_client.adapters.platforms.windows.native import canonical

        canonical(path)
        (cache_directory if cache else private_directory)(path.parent)
        from agent_brain_client.adapters.platforms.windows.filesystem import open_lock

        fd = open_lock(path, cache=cache)
        try:
            handle = os.fdopen(fd, "r+b")
        except BaseException:
            os.close(fd)
            raise
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            handle = path.open("r+b")
        else:
            handle = os.fdopen(fd, "r+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            from agent_brain_client.adapters.platforms.windows.filesystem import (
                private_handle,
            )

            private_handle(
                msvcrt.get_osfhandle(handle.fileno()),
                owner=True,
                check_access=not cache,
            )
        # LockFileEx supports ranges beyond EOF. Writing an initialization byte
        # before locking races another caller that already owns that byte.
        if os.name != "nt" and path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            if cancel_check is not None:
                cancel_check()
            try:
                handle.seek(0)
                if os.name == "nt":
                    from agent_brain_client.adapters.platforms.windows.native import (
                        file_lock,
                    )

                    file_lock(handle, shared=shared)
                else:
                    import fcntl

                    fcntl.flock(
                        handle.fileno(),
                        (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB,
                    )
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout("Another operation is running; retry shortly.")
                time.sleep(0.1)
        if cancel_check is not None:
            cancel_check()
        yield handle
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                from agent_brain_client.adapters.platforms.windows.native import (
                    file_lock,
                )

                file_lock(handle, release=True)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
