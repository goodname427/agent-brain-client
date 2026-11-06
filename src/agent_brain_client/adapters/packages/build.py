"""Assemble the exact public runtime from this independent repository."""

import hashlib
import json
from pathlib import Path
from agent_brain_client.adapters.packages.client import (
    CLIENT_FILES,
    CONTENT_CLIENT_FILES,
    SSH_CONTENT_CLIENT_FILES,
    HTTPS_CONTENT_CLIENT_FILES,
    WINDOWS_HTTPS_CONTENT_CLIENT_FILES,
    CAPABILITIES,
    PERSONAL_CAPABILITIES,
    CONTENT_CAPABILITIES,
    SSH_CONTENT_CAPABILITIES,
    HTTPS_CONTENT_CAPABILITIES,
)
from agent_brain_client.adapters.packages.publication import require_publishable
from agent_brain_client.adapters.packages.releases import version as validate_version

ROOT = Path(__file__).resolve().parents[4]


def export(
    source,
    output,
    pairing_candidate=False,
    content_candidate=False,
    ssh_content_candidate=False,
    https_content_candidate=False,
    target_platform=None,
):
    if Path(source).resolve() != ROOT:
        raise ValueError(
            "Build only the independent Client repository; external source prohibited"
        )
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Existing artifact preserved")
    if https_content_candidate and any(
        (pairing_candidate, content_candidate, ssh_content_candidate)
    ):
        raise ValueError("Separate HTTPS capability candidate required")
    if ssh_content_candidate and any((pairing_candidate, content_candidate)):
        raise ValueError("Separate reviewed transport capability required")
    names = (
        (
            WINDOWS_HTTPS_CONTENT_CLIENT_FILES
            if target_platform and target_platform.startswith("win32-")
            else HTTPS_CONTENT_CLIENT_FILES
        )
        if https_content_candidate
        else SSH_CONTENT_CLIENT_FILES
        if ssh_content_candidate
        else CONTENT_CLIENT_FILES
        if content_candidate
        else CLIENT_FILES
        + (
            "src/agent_brain_client/application/installation/pairing.py",
            "src/agent_brain_client/adapters/platforms/macos/native/DeviceComponent.swift",
        )
        if pairing_candidate
        else CLIENT_FILES
    )
    prior = ()
    payload = {}
    for name in names:
        p = ROOT / name
        if not p.is_file() or p.is_symlink():
            raise ValueError("Missing or linked local source")
        payload[name] = p.read_bytes()
    require_publishable(payload)
    output.mkdir(parents=True)
    for name, data in payload.items():
        p = output / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    manifest = {
        "schema": 1,
        "review_status": "unpublished-candidate",
        "files": {n: hashlib.sha256(v).hexdigest() for n, v in payload.items()},
        "git_history_copied": False,
        "core_exported": False,
        "native_install_wired": not any(
            (
                pairing_candidate,
                content_candidate,
                ssh_content_candidate,
                https_content_candidate,
            )
        ),
        "actual_deployment": "not-performed-by-build",
        "source_authority": "independent-client-repository",
        "release_policy_scope_delta": {
            "added": sorted(set(names) - set(prior)),
            "removed": sorted(set(prior) - set(names)),
        },
        "production_release_policy_scope_compatibility": "current-layout-reinstall",
    }
    if https_content_candidate:
        manifest.update(
            content="https-system-credential-uninstalled-candidate",
            production_release_policy_compatible=False,
            production_release_policy_compatibility_reason="Candidate has not been installed or deployed; production compatibility is not claimed.",
            credential_helper_installed=False,
            keychain_accessed=False,
        )
    if ssh_content_candidate:
        manifest.update(
            content="ssh-server-credential-uninstalled-candidate",
            production_release_policy_compatible=False,
            production_release_policy_compatibility_reason="Candidate has not been installed or deployed; production compatibility is not claimed.",
        )
    if pairing_candidate or content_candidate:
        manifest.update(
            pairing="source-only-uninstalled-unsigned-candidate",
            production_release_policy_compatible=False,
            production_release_policy_compatibility_reason="Candidate has not been installed or deployed; production compatibility is not claimed.",
            existing_ssh_checkpoint_available=True,
        )
    (output / "export-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def release(source, output, version, data_mode="synthetic", target_platform=None):
    modes = {
        "synthetic": CAPABILITIES,
        "personal": PERSONAL_CAPABILITIES,
        "personal-content": CONTENT_CAPABILITIES,
        "personal-content-ssh": SSH_CONTENT_CAPABILITIES,
        "personal-content-https": HTTPS_CONTENT_CAPABILITIES,
    }
    if data_mode not in modes:
        raise ValueError("Unknown reviewed data mode")
    validate_version(version)
    built = export(
        source,
        output,
        content_candidate=data_mode == "personal-content",
        ssh_content_candidate=data_mode == "personal-content-ssh",
        https_content_candidate=data_mode == "personal-content-https",
        target_platform=target_platform,
    )
    manifest = {
        "schema": 1,
        "version": version,
        "channel": "stable",
        "core_api": 1,
        "state_schema": 1,
        "python_min": "3.11",
        "capabilities": modes[data_mode],
        "files": built["files"],
        "migrations": [],
    }
    p = Path(output) / "releases/manifest.json"
    p.parent.mkdir()
    p.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return manifest
