"""Bounded, link-safe nonsecret configuration JSON."""

import hashlib
import json
from agent_brain_client.adapters.platforms.source_files import canonical


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate configuration field")
        value[key] = item
    return value


def read_json(path, expected=None):
    path = canonical(path)
    if not path.is_file() or path.stat().st_nlink != 1 or path.stat().st_size > 1048576:
        raise ValueError("Bounded Client metadata required")
    raw = path.read_bytes()
    if expected is not None and digest(raw) != expected:
        raise ValueError("Client metadata changed")
    return json.loads(raw, object_pairs_hook=unique)
