"""Bounded, link-safe nonsecret configuration JSON."""

import hashlib
import json
import os
from pathlib import Path


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate configuration field")
        value[key] = item
    return value


def canonical(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Linked Client path preserved")
    if os.name == "nt" and any(
        p.exists() and getattr(p.lstat(), "st_file_attributes", 0) & 0x400
        for p in (path, *path.parents)
    ):
        raise ValueError("Reparse Client path preserved")
    return path


def read_json(path, expected=None):
    path = canonical(path)
    if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 1048576:
        raise ValueError("Bounded Client metadata required")
    raw = path.read_bytes()
    if expected is not None and digest(raw) != expected:
        raise ValueError("Client metadata changed")
    return json.loads(raw, object_pairs_hook=unique)
