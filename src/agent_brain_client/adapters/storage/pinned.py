"""Bounded private JSON pinned by its exact original byte digest."""

import hashlib
import json
from pathlib import Path
from agent_brain_client.adapters.platforms.filesystem import (
    require_platform,
    private_mode,
)
from agent_brain_client.adapters.storage.metadata import unique as pairs


def pinned(path, digest):
    require_platform("private-metadata")
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError()
    private_mode(path)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError()
    return json.loads(raw, object_pairs_hook=pairs)
