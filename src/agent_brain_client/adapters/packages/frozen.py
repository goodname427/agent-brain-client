"""Immutable runtime snapshots shared by every Harness."""

import json
from pathlib import Path
from agent_brain_client.adapters.storage import metadata as metadata_io
from agent_brain_client.adapters.platforms.filesystem import private_directory, private_mode

ROOT = Path(__file__).resolve().parents[4]


def freeze_code(directory, pins, *, source=ROOT):
    """Keep executable Hook code in a private immutable cache, never a checkout."""
    from agent_brain_client.adapters.storage.host_files import atomic

    root = (
        directory
        / "code"
        / metadata_io.digest(json.dumps(pins, sort_keys=True).encode())
    )
    metadata_io.canonical(root)  # Reject linked ancestors before creating any cache.
    for parent in (directory / "code", root):
        private_directory(parent)
        private_mode(parent, directory=True, owner=True)
    for name, pin in pins.items():
        target = root / name
        metadata_io.canonical(target)
        private_directory(target.parent)
        private_mode(target.parent, directory=True, owner=True)
        if target.exists():
            metadata_io.canonical(target, existing=True)
            private_mode(target, owner=True)
            if (
                target.stat().st_nlink != 1
                or metadata_io.digest(target.read_bytes()) != pin
            ):
                raise ValueError("Frozen global code changed")
            continue
        raw = (source / name).read_bytes()
        if metadata_io.digest(raw) != pin:
            raise ValueError("Global update source changed")
        atomic(target, raw, 0o600)
    return root
