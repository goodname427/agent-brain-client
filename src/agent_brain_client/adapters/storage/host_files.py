"""Host-neutral bounded file transactions and private atomic storage."""

import base64
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
from agent_brain_client.adapters.platforms.filesystem import require_platform
from agent_brain_client.adapters.platforms.host_files import (
    inplace_host_updates,
    open_host_file,
    validate_host_path,
    host_metadata,
    prepare_host_mode,
    publish_new_host_file,
    prepare_atomic_mode,
    finish_atomic_mode,
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class HostConfigChanged(ValueError):
    """A staged host write was cancelled before modifying the destination."""


def atomic(path, raw, mode, *, cache=False):
    require_platform("private-write")
    from agent_brain_client.adapters.platforms.filesystem import (
        private_directory,
        atomic_replace,
    )
    import agent_brain_client.adapters.platforms.filesystem as brain_lock

    directory = brain_lock.cache_directory if cache else private_directory
    directory(path.parent)
    fd, name = tempfile.mkstemp(prefix=".client-native-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            prepare_atomic_mode(file, mode, cache=cache)
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        finish_atomic_mode(name, mode)
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
    validate_host_path(path)
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    if not path.is_file() or st.st_nlink != 1 or st.st_size > 1048576:
        raise ValueError("Single bounded host file required")
    metadata = host_metadata(st)
    return (path.read_bytes(), st.st_dev, st.st_ino, st.st_mtime_ns, metadata)


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
    if inplace_host_updates() and image is not None and not private:
        with open_host_file(path) as file:
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
            prepare_host_mode(file, mode, image, private=private)
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        staged = host_image(pending)

        def commit():
            if host_image(path) != image or host_image(pending) != staged:
                raise HostConfigChanged("Host configuration concurrently changed")
            if image is None:
                publish_new_host_file(pending, path)
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
