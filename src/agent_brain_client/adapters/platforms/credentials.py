"""Shared credential records, metadata validation and OS backend selection."""

import hashlib
import json
import os
from pathlib import Path
import re

from agent_brain_client.adapters.platforms.filesystem import (
    file_mode,
    locked,
    private_mode,
    require_platform,
    platform_paths,
    WINDOWS_PLATFORMS,
    private_directory,
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate credential metadata")
        result[key] = value
    return result


def owned(path, *, directory=False):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Owned canonical path required")
    return private_mode(path, directory=directory, owner=True)


class CredentialStore:
    def __init__(self, backend, state_directory):
        self.backend, self.directory = backend, Path(state_directory)

    def lease(self):
        # Only private non-secret locks live on disk. The entire enrollment
        # holds one lease, so concurrent starts cannot replace each other's item.
        require_platform("credential-store")
        private_directory(self.directory)
        owned(self.directory, directory=True)
        path = self.directory / "pairing.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                file_mode(fd, 0o600)
            finally:
                os.close(fd)
        except FileExistsError:
            pass
        owned(path)
        return locked(path, timeout=0)

    def load(self):
        return self.backend.load()

    def save(self, record, expected):
        self.backend.save(record, expected)

    def clear(self, expected):
        self.backend.clear(expected)


def credential_metadata(target, descriptor):
    """Validate nonsecret backend metadata separately from choosing a real store."""
    paths = platform_paths(target)
    if target in WINDOWS_PLATFORMS:
        if (
            not isinstance(descriptor, dict)
            or set(descriptor) != {"kind", "source_sha256"}
            or descriptor["kind"] != "windows-credential-manager"
            or not re.fullmatch(r"[a-f0-9]{64}", descriptor["source_sha256"])
        ):
            raise ValueError("Pinned Windows credential backend required")
        return descriptor
    keys = {"kind", "helper", "helper_sha256", "source_sha256"}
    if (
        not isinstance(descriptor, dict)
        or set(descriptor) != keys
        or descriptor["kind"] != "mac-login-keychain"
    ):
        raise ValueError("Reviewed native credential metadata required")
    helper = paths(descriptor["helper"])
    if not helper.is_absolute() or ".." in helper.parts:
        raise ValueError("Absolute credential helper required")
    if any(
        not re.fullmatch(r"[a-f0-9]{64}", descriptor[k])
        for k in ("helper_sha256", "source_sha256")
    ):
        raise ValueError("Credential pins required")
    return descriptor


def credential_fields(target, descriptor):
    credential_metadata(target, descriptor)
    if target in WINDOWS_PLATFORMS:
        return {"schema": 2, "credential_backend": descriptor}
    return {
        "schema": 1,
        "credential_helper": descriptor["helper"],
        "credential_helper_sha256": descriptor["helper_sha256"],
        "credential_source_sha256": descriptor["source_sha256"],
    }


def profile_credentials(value):
    target = value["platform"]
    platform_paths(target)
    if type(value.get("schema")) is not int:
        raise ValueError("Credential metadata schema required")
    if target in WINDOWS_PLATFORMS:
        if value["schema"] != 2:
            raise ValueError("Explicit credential backend required")
        descriptor = value["credential_backend"]
    else:
        if value["schema"] != 1:
            raise ValueError("Legacy native credential metadata required")
        descriptor = {
            "kind": "mac-login-keychain",
            "helper": value["credential_helper"],
            "helper_sha256": value["credential_helper_sha256"],
            "source_sha256": value["credential_source_sha256"],
        }
    return credential_metadata(target, descriptor)


def credential_paths(profile):
    require_platform("credential-store", profile["platform"])
    for key in (
        ("state_directory",)
        if profile["platform"] in WINDOWS_PLATFORMS
        else ("credential_helper", "state_directory")
    ):
        p = Path(profile[key])
        if any(item.is_symlink() for item in (p, *p.parents)):
            raise ValueError("Canonical credential runtime required")


def store_for_profile(profile, profile_sha):
    require_platform("credential-store", profile.get("platform"))
    if profile["platform"] in WINDOWS_PLATFORMS:
        import agent_brain_client.adapters.platforms.windows.native as brain_windows

        descriptor = profile_credentials(profile)
        if (
            hashlib.sha256(Path(brain_windows.__file__).read_bytes()).hexdigest()
            != descriptor["source_sha256"]
        ):
            raise ValueError(
                "Windows credential backend changed; profile review required"
            )
        backend = brain_windows.WindowsCredentials("agent-brain.https." + profile_sha)
    else:
        from agent_brain_client.adapters.platforms.macos.keychain import NativeKeychain

        backend = NativeKeychain(
            profile["credential_helper"],
            profile["credential_helper_sha256"],
            "agent-brain.https." + profile_sha,
        )
    return CredentialStore(backend, profile["state_directory"])
