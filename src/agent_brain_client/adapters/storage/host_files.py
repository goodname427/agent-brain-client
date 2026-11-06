"""Host-neutral bounded file transactions and private atomic storage."""

import base64
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
from agent_brain_client.adapters.platforms.filesystem import require_platform


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class HostConfigChanged(ValueError):
    """A staged host write was cancelled before modifying the destination."""


def atomic(path, raw, mode, *, cache=False):
    require_platform("private-write")
    from agent_brain_client.adapters.platforms.filesystem import (
        private_directory,
        atomic_replace,
        file_mode,
    )
    import agent_brain_client.adapters.platforms.filesystem as brain_lock

    directory = (
        getattr(brain_lock, "cache_directory", private_directory)
        if cache
        else private_directory
    )
    directory(path.parent)
    fd, name = tempfile.mkstemp(prefix=".client-native-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            if not cache or os.name != "nt":
                file_mode(file.fileno(), mode)
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        if os.name != "nt":
            os.chmod(name, mode)
        atomic_replace(name, path)
    finally:
        if Path(name).exists():
            Path(name).unlink()


def host_image(path):
    """Nonsecret host preflight; never read or rewrite its security descriptor."""
    path = Path(path)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or any(p.is_symlink() for p in (path, *path.parents))
    ):
        raise ValueError("Canonical host path required")
    if os.name == "nt":
        from agent_brain_client.adapters.platforms.windows.native import canonical

        canonical(path)
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    if not path.is_file() or st.st_nlink != 1 or st.st_size > 1048576:
        raise ValueError("Single bounded host file required")
    metadata = (
        getattr(st, "st_file_attributes", 0)
        if os.name == "nt"
        else (st.st_uid, st.st_gid, st.st_mode & 0o777)
    )
    return (path.read_bytes(), st.st_dev, st.st_ino, st.st_mtime_ns, metadata)


@contextmanager
def windows_host_file(path):
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


def host_write(file, raw):
    """Write a prefix before truncating, so journaled torn writes are bounded."""
    file.seek(0)
    offset = 0
    while offset < len(raw):
        written = file.write(raw[offset:])
        if not written:
            raise OSError("Host write incomplete")
        offset += written
    file.flush()
    os.fsync(file.fileno())
    file.truncate(len(raw))
    file.flush()
    os.fsync(file.fileno())


@contextmanager
def host_stage(path, raw, mode, expected, *, private=False):
    """Preflight host writes without applying the private-store policy.

    Windows existing files keep their object (and unknown SACL/owner/streams).
    The caller must flush its write-ahead journal before commit. Readers may
    observe a partial write; the kernel excludes competing writers and renames.
    New files and POSIX writes use staged, atomic publication instead.
    """
    require_platform("native-install")
    from agent_brain_client.adapters.platforms.filesystem import (
        atomic_replace,
        sync_directory,
        private_directory,
        file_mode,
    )

    path = Path(path)
    image = host_image(path)
    if private:
        # Only caller-owned Client metadata uses this branch. Existing host
        # files retain their object/security; private metadata uses the same
        # protected atomic replacement as the private store, with a CAS image.
        private_directory(path.parent)
    if (image[0] if image is not None else None) != expected:
        raise HostConfigChanged("Host configuration concurrently changed")
    if os.name == "nt" and image is not None and not private:
        with windows_host_file(path) as file:
            st = os.fstat(file.fileno())
            original = file.read(1048577)
            if original != expected or (st.st_dev, st.st_ino) != image[1:3]:
                raise HostConfigChanged("Host configuration concurrently changed")

            def commit_inplace():
                file.seek(0)
                if file.read(1048577) != expected or host_image(path) != image:
                    raise HostConfigChanged("Host configuration concurrently changed")
                host_write(file, raw)

            setattr(
                commit_inplace,
                "inplace_identity",
                {"device": st.st_dev, "inode": st.st_ino},
            )
            yield commit_inplace
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    host_image(path)
    fd, name = tempfile.mkstemp(prefix=".client-host-", dir=path.parent)
    pending = Path(name)
    try:
        with os.fdopen(fd, "wb") as file:
            if private and os.name == "nt":
                file_mode(file.fileno(), mode)
            if os.name != "nt":
                if image is not None:
                    st = pending.stat()
                    if (st.st_uid, st.st_gid) != image[4][:2]:
                        os.fchown(file.fileno(), *image[4][:2])
                os.fchmod(file.fileno(), mode)
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        staged = host_image(pending)

        def commit():
            if host_image(path) != image or host_image(pending) != staged:
                raise HostConfigChanged("Host configuration concurrently changed")
            if image is None:
                if os.name == "nt":
                    os.rename(pending, path)
                else:
                    os.link(pending, path)
                    pending.unlink()
            else:
                atomic_replace(pending, path)
            sync_directory(path.parent)

        yield commit
    finally:
        pending.unlink(missing_ok=True)


def host_atomic(path, raw, mode, expected, *, private=False):
    options = {"private": True} if private else {}
    with host_stage(path, raw, mode, expected, **options) as commit:
        commit()


def host_plan(payload, *, private_paths=()):
    """Bounded data images for nonsecret host files; journal stays private."""
    rows = {}
    private_paths = set(map(str, private_paths))
    if private_paths - set(map(str, payload)):
        raise ValueError("Private metadata outside transaction")
    for path, target in payload.items():
        image = host_image(path)
        before = image[0] if image else None
        if target is not None and (
            not isinstance(target, bytes) or len(target) > 1048576
        ):
            raise ValueError("Bounded host target required")
        rows[str(path)] = {
            "before": base64.b64encode(before).decode() if before is not None else None,
            "target": base64.b64encode(target).decode() if target is not None else None,
            "before_sha": sha(before) if before is not None else None,
            "target_sha": sha(target) if target is not None else None,
            "mode": path.stat().st_mode & 0o777 if image else 0o600,
        }
        if str(path) in private_paths:
            rows[str(path)]["private"] = True
    return rows


def host_row(path, row):
    keys = {"before", "target", "before_sha", "target_sha", "mode"}
    if (
        not keys <= set(row)
        or set(row) - keys - {"identity", "rollback", "private"}
        or "private" in row
        and row["private"] is not True
        or type(row["mode"]) is not int
        or row["mode"] & ~0o777
    ):
        raise ValueError("Host journal changed")
    before, target = (
        base64.b64decode(row[k], validate=True) if row[k] is not None else None
        for k in ("before", "target")
    )
    if (
        any(v is not None and len(v) > 1048576 for v in (before, target))
        or (sha(before) if before is not None else None) != row["before_sha"]
        or (sha(target) if target is not None else None) != row["target_sha"]
    ):
        raise ValueError("Host journal images changed")
    image = host_image(path)
    current = image[0] if image else None
    identity = row.get("identity")
    if identity is not None:
        if (
            not isinstance(identity, dict)
            or set(identity) != {"device", "inode"}
            or any(type(v) is not int or v < 0 for v in identity.values())
            or image is None
            or image[1:3] != (identity["device"], identity["inode"])
            or before is None
            or target is None
        ):
            raise ValueError("Host recovery object changed")
        intent = row.get("rollback")
        if intent is not None:
            if not isinstance(intent, dict) or set(intent) != {"data", "sha"}:
                raise ValueError("Host rollback intent changed")
            torn = base64.b64decode(intent["data"], validate=True)
            if (
                sha(torn) != intent["sha"]
                or partial_prefix(before, target, torn) is None
                or partial_prefix(torn, before, current) is None
            ):
                raise ValueError("Outside host edit preserved")
        elif partial_prefix(before, target, current) is None:
            raise ValueError("Outside host edit preserved")
    elif current not in (before, target) or "rollback" in row:
        raise ValueError("Outside host edit preserved")
    return before, target, current


def partial_prefix(before, after, current):
    if current == after:
        return len(after)
    offset = 0
    for old, new in zip(current, after):
        if old != new:
            break
        offset += 1
    if (
        len(current) == max(len(before), offset)
        and current == after[:offset] + before[offset:]
    ):
        return offset
    return None


def host_apply(rows, persist):
    """Persist exact intent before data writes, including Windows file identity."""
    from contextlib import ExitStack

    with ExitStack() as staging:
        commits = {}
        for name, row in rows.items():
            path = Path(name)
            if (host_image(path) or (None,))[0] != (
                base64.b64decode(row["before"], validate=True)
                if row["before"] is not None
                else None
            ):
                raise HostConfigChanged("Host configuration concurrently changed")
            before, target, current = host_row(path, row)
            if current != before:
                raise ValueError("Host preimage changed")
            if target is not None:
                options = {"private": True} if row.get("private") else {}
                commit = staging.enter_context(
                    host_stage(path, target, row["mode"], before, **options)
                )
                if hasattr(commit, "inplace_identity"):
                    row["identity"] = commit.inplace_identity
                commits[name] = commit
        persist()
        for name, row in rows.items():
            if name in commits:
                commits[name]()
            else:
                path = Path(name)
                if (host_image(path) or (None,))[0] != (
                    base64.b64decode(row["before"])
                    if row["before"] is not None
                    else None
                ):
                    raise ValueError("Host removal preimage changed")
                path.unlink(missing_ok=True)


def host_restore(rows, persist):
    # Callers validate their exact permitted path set and image hashes first.
    for name, row in rows.items():
        host_row(Path(name), row)
    for name, row in rows.items():
        path = Path(name)
        before, target, current = host_row(path, row)
        if current == before:
            continue
        if before is None:
            path.unlink(missing_ok=True)
        else:
            if "identity" in row and "rollback" not in row:
                row["rollback"] = {
                    "data": base64.b64encode(current).decode(),
                    "sha": sha(current),
                }
                persist()
            options = {"private": True} if row.get("private") else {}
            host_atomic(path, before, row["mode"], current, **options)
