"""Owned metadata I/O, shared by all Harnesses without loading one."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
from agent_brain_client.adapters.platforms.filesystem import (
    cache_mode,
    private_mode,
    file_mode,
    atomic_replace,
    sync_directory,
)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unique(pairs):
    value = {}
    for k, v in pairs:
        if k in value:
            raise ValueError("Duplicate settings field")
        value[k] = v
    return value


def canonical(path, *, existing=False):
    p = Path(path)
    if (
        not p.is_absolute()
        or ".." in p.parts
        or any(x.is_symlink() for x in (p, *p.parents))
        or existing
        and not p.exists()
    ):
        raise ValueError("Canonical absolute path required")
    return p


def private_json(path, sha=None, *, cache=False, max_bytes=256 * 1024):
    p = canonical(path, existing=True)
    if cache:
        cache_mode(p, owner=True)
    else:
        private_mode(p, owner=True)
    if p.stat().st_size > max_bytes:
        raise ValueError("Private owned metadata required")
    with p.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("Private owned metadata required")
    if sha is not None and digest(raw) != sha:
        raise ValueError("Metadata digest changed")
    return json.loads(raw, object_pairs_hook=unique)


def write(path, value, *, exclusive=False, mode=0o600, cache=False, max_bytes=None):
    p = canonical(path)
    raw = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()
    if max_bytes is not None and len(raw) > max_bytes:
        raise ValueError("Private metadata exceeds its size limit")
    if exclusive:
        with p.open("xb") as stream:
            if not cache or os.name != "nt":
                file_mode(stream.fileno(), mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        fd, pending = tempfile.mkstemp(prefix="." + p.name + "-", dir=p.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                if not cache or os.name != "nt":
                    file_mode(stream.fileno(), mode)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            atomic_replace(pending, p)
        finally:
            if os.path.exists(pending):
                os.unlink(pending)
    sync_directory(p.parent)
    return digest(raw)
