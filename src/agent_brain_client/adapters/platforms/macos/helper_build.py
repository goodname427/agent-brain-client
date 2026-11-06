"""Compile and self-check the pinned credential helper; no Keychain access."""

import hashlib
import json
import subprocess
from pathlib import Path
from agent_brain_client.adapters.platforms.filesystem import (
    require_platform,
    MAC_PLATFORMS,
)

ROOT = Path(__file__).resolve().parents[5]


def build(output, *, access=False):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Existing helper preserved")
    target = require_platform("credential-helper-build")
    if target not in MAC_PLATFORMS:
        raise ValueError("macOS Swift build host required")
    machine = target.removeprefix("darwin-")
    cache = ROOT / ".local/swift-module-cache"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = ROOT / (
        "src/agent_brain_client/adapters/platforms/macos/native/AccessCredentialStore.swift"
        if access
        else "src/agent_brain_client/adapters/platforms/macos/native/CredentialStore.swift"
    )
    subprocess.run(
        [
            "/usr/bin/swiftc",
            "-O",
            "-target",
            machine + "-apple-macos13.0",
            "-module-cache-path",
            str(cache),
            str(source),
            "-o",
            str(output),
        ],
        check=True,
    )
    output.chmod(0o700)
    run = subprocess.run(
        [str(output), "--self-check"], capture_output=True, text=True, check=True
    )
    check = json.loads(run.stdout)
    return {
        "schema": 1,
        "platform": "Darwin-" + machine,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "helper_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "self_check": check,
        "installed": False,
        "keychain_accessed": False,
        "signing_identity_created": False,
    }
