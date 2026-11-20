"""Immutable runtime snapshots shared by every Harness."""

import json
import re
from pathlib import Path
from agent_brain_client.adapters.storage import metadata as metadata_io
from agent_brain_client.adapters.storage.bounded_json import read_json
from agent_brain_client.adapters.packages.layout import (
    PYTHON_FILES,
    runtime_python_members,
    source_python_members,
)
from agent_brain_client.adapters.platforms.filesystem import private_directory, private_mode

ROOT = Path(__file__).resolve().parents[4]


def validate_python_pins(pins, *, required=PYTHON_FILES):
    if not isinstance(pins, dict) or not pins or any(
        not isinstance(pin, str) or not re.fullmatch("[a-f0-9]{64}", pin)
        for pin in pins.values()
    ):
        raise ValueError("Complete Python digest inventory required")
    runtime_python_members(pins, required=required)
    return pins


def runtime_pins(source=ROOT, *, fallback=PYTHON_FILES, manifest_sha256=None):
    """Read pins from a verified package, frozen image or trusted local source."""
    source = Path(source)
    verified = source / "verified.json"
    frozen = source / "code.json"
    exported = source / "export-manifest.json"
    if verified.exists() or verified.is_symlink() or manifest_sha256 is not None:
        manifest = read_json(verified)["manifest"]
        if manifest_sha256 is not None and metadata_io.digest(
            json.dumps(manifest, sort_keys=True).encode()
        ) != manifest_sha256:
            raise ValueError("Verified package manifest changed")
        files = manifest["files"]
    elif frozen.exists() or frozen.is_symlink():
        value = read_json(frozen)
        if set(value) != {"schema", "files"} or value["schema"] != 1:
            raise ValueError("Frozen Python inventory changed")
        files = value["files"]
        if source.name != metadata_io.digest(json.dumps(files, sort_keys=True).encode()):
            raise ValueError("Frozen Python inventory escaped its digest directory")
    elif exported.exists() or exported.is_symlink():
        files = read_json(exported)["files"]
    else:
        # Historical caches have no inventory sidecar and keep their original set.
        # The actual installer source checkout can enumerate reviewed source files.
        names = (
            source_python_members(source)
            if fallback == PYTHON_FILES and (source / "dev/build/package.py").is_file()
            else fallback
        )
        return {name: metadata_io.digest((source / name).read_bytes()) for name in names}
    pins = {name: pin for name, pin in files.items() if name.endswith(".py")}
    return validate_python_pins(pins, required=fallback)


def frozen_source_files(root):
    return {
        path.relative_to(root).as_posix()
        for path in (root / "src").rglob("*")
        if not metadata_io.canonical(path).is_dir()
    }


def verify_frozen_code(root):
    """Read the digest-named inventory and verify every member of the image."""
    pins = runtime_pins(root)
    if frozen_source_files(root) != pins.keys():
        raise ValueError("Frozen Python code scope changed")
    for name, pin in pins.items():
        path = metadata_io.canonical(root / name)
        if path.stat().st_nlink != 1 or metadata_io.digest(path.read_bytes()) != pin:
            raise ValueError("Frozen Python code changed")
    return pins


def compatible_codebuddy_runtime(home, executing, executing_pins, configured, configured_pins):
    """Only a pinned CodeBuddy image can use worker-only runtime compatibility."""
    from agent_brain_client.adapters.packages.client import compatible_managed_workers

    directory = Path(home) / ".config/agent-brain-client"
    if (
        executing.parent != directory / "codebuddy/code"
        or configured.parent != directory / "global-memory/code"
        or not (executing / "code.json").is_file()
    ):
        return False
    return compatible_managed_workers(executing, executing_pins, configured, configured_pins)


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
    expanded = bool(pins.keys() - set(PYTHON_FILES))
    if expanded:
        validate_python_pins(pins)
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
    if expanded:
        if frozen_source_files(root) != pins.keys():
            raise ValueError("Unlisted frozen Python file")
        record = root / "code.json"
        value = {"schema": 1, "files": pins}
        if record.exists() or record.is_symlink():
            if read_json(record) != value:
                raise ValueError("Frozen Python inventory changed")
        else:
            metadata_io.write(record, value)
    return root
