"""Fixed approved MCP startup: serve a verified snapshot, coordinate one check.

Deploy this bootstrap and its dependencies in a reviewed fixed directory; pass
the policy digest through the forced SSH command. No daemon or native rewriting.
"""

import sys
from pathlib import Path

import hashlib
import json
import os
import subprocess
import threading

from agent_brain_client.adapters.packages.client import (
    ClientReleases,
    CONTENT_CAPABILITIES,
    SSH_CONTENT_CAPABILITIES,
    HTTPS_CONTENT_CAPABILITIES,
    LEGACY_PRESERVATION,
    DEPENDENCY_RUNTIME,
    version,
)
from agent_brain_client.adapters.platforms.filesystem import (
    file_mode,
    require_platform,
    PlatformUnsupported,
)
import agent_brain_client.adapters.platforms.filesystem as brain_lock
from agent_brain_client.adapters.storage.reinstallation import require_memory_verified
from agent_brain_client.application.installation.fresh import (
    FreshMemoryVerificationRequired,
)

cache_directory = brain_lock.cache_directory
cache_mode = brain_lock.cache_mode
cache_locked = brain_lock.cache_locked

OUTBOX_MIN_VERSION = "0.2.1"


def content_runtime_blocker(active, policy):
    """Old content runtimes cannot interpret the new private local ledgers.

    Check existence only: retain even empty, damaged or linked state as evidence.
    This guard belongs to the reviewed fixed bootstrap, outside rollback packages.
    """
    if policy["capabilities"] not in (
        CONTENT_CAPABILITIES,
        SSH_CONTENT_CAPABILITIES,
        HTTPS_CONTENT_CAPABILITIES,
    ) or version(active["version"]) >= version(OUTBOX_MIN_VERSION):
        return None
    cache = Path(policy["runtime"]["content_cache"])
    found = False
    try:
        for name in ("outbox", "imports.json"):
            try:
                (cache / name).lstat()
            except FileNotFoundError:
                continue
            found = True
            break
    except OSError:
        return {
            "status": "content-local-state-unverified",
            "served": False,
            "native_changed": False,
            "required_client_version": OUTBOX_MIN_VERSION,
            "next_action": "Local content state could not be checked. Preserve the content cache and restore access or an outbox-compatible verified client before serving.",
        }
    if found:
        return {
            "status": "content-outbox-runtime-incompatible",
            "served": False,
            "native_changed": False,
            "required_client_version": OUTBOX_MIN_VERSION,
            "next_action": "Local outbox or import inventory is retained, but the active client cannot safely handle it. Restore a verified client version 0.2.1 or later; do not delete local state to enable the old runtime.",
        }
    return None


def serve_content(command, active, policy, status):
    """Keep the fixed guard valid until the actual mapping worker exits.

    Legacy workers take an exclusive lease; compatible workers share a lease.
    The child inherits the descriptor, so a killed bootstrap cannot release its
    live worker's protection. Persistent lock files are never removed/reset.
    """
    try:
        require_platform("worker-lease", policy.get("platform"))
    except PlatformUnsupported as error:
        return error.result(served=False, native_changed=False)
    cache = Path(policy["runtime"]["content_cache"])
    try:
        if cache.is_symlink():
            raise ValueError("Linked content cache")
        cache_directory(cache)
        cache_mode(cache, directory=True, owner=True)
        path = cache / ".content-runtime.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                if os.name != "nt":
                    file_mode(fd, 0o600)
            finally:
                os.close(fd)
        except FileExistsError:
            pass
        if path.is_symlink():
            raise ValueError("Content lease changed")
        cache_mode(path, owner=True)
        modern = version(active["version"]) >= version(OUTBOX_MIN_VERSION)
        with cache_locked(path, timeout=0, shared=modern) as lease:
            try:
                require_memory_verified(
                    policy["runtime"]["device_home"],
                    policy["runtime"].get("https_profile_sha256"),
                )
            except FreshMemoryVerificationRequired as error:
                return error.result()
            from agent_brain_client.adapters.harnesses.codex.installation import inspect

            if not inspect(policy["runtime"]["device_home"]).get("installed"):
                return {
                    "status": "content-native-install-pending",
                    "served": False,
                    "native_changed": False,
                    "next_action": "The client was removed or changed before the worker lease. Reinstall or repair using the public entry.",
                }
            blocked = content_runtime_blocker(active, policy)
            if blocked:
                return blocked
            return {
                "status": status,
                "served": True,
                "exit_code": subprocess.run(
                    command,
                    **({} if os.name == "nt" else {"pass_fds": (lease.fileno(),)}),
                ).returncode,
            }
    except RuntimeError:
        return {
            "status": "content-runtime-in-use",
            "served": False,
            "native_changed": False,
            "next_action": "An incompatible content worker still owns this cache. Preserve it and retry after that worker exits; no worker or local ledger is stopped, removed or reset.",
        }
    except (OSError, ValueError):
        return {
            "status": "content-runtime-lifecycle-review-required",
            "served": False,
            "native_changed": False,
            "next_action": "Preserve the content cache and its persistent lease; inspect ownership or access before serving.",
        }


def startup(
    policy_path,
    policy_sha256,
    check_only=False,
    installation_check=False,
    bootstrap_check=False,
    event_hook=None,
):
    releases = ClientReleases(policy_path, policy_sha256)
    if bootstrap_check:
        active = releases.verified_active()
        if not active:
            return {
                "status": "client-bootstrap-review-required",
                "served": False,
                "native_changed": False,
            }
        return {
            "status": "client-bootstrap-verified",
            "active_package_version": active["version"],
            "active_package_commit": active.get("commit"),
            "active_package": active.get("package"),
            "native_changed": False,
            "network_used": False,
            "device_credentials_accessed": False,
        }
    runtime = (
        releases.policy.get("runtime") if isinstance(releases.policy, dict) else None
    )
    device_home = runtime.get("device_home") if isinstance(runtime, dict) else None
    if isinstance(device_home, str) and not (check_only or installation_check):
        try:
            require_memory_verified(device_home, runtime.get("https_profile_sha256"))
        except FreshMemoryVerificationRequired as error:
            return error.result()
    if isinstance(device_home, str):
        handover = (
            Path(device_home) / ".config/agent-brain-client/launcher-handover.json"
        )
        if handover.exists() or handover.is_symlink():
            return {
                "status": "client-bootstrap-handover-in-progress",
                "served": False,
                "native_changed": False,
                "next_action": "Resume the public Windows install/update entry with its saved launcher transaction.",
            }
    if event_hook:
        # Per-turn capture must never install, refresh or move an active release.
        # Constructor and verified_active retain the normal release trust chain.
        if event_hook and (check_only or installation_check or bootstrap_check):
            raise ValueError("Distinct hook mode required")
        active = releases.verified_active(serving=True)
        result = {"status": "current", "active": active}
    elif check_only or installation_check:
        result = releases.update()
    else:
        try:
            active = releases.verified_active(serving=True)
        except Exception:
            return {
                "status": "client-startup-retry-required",
                "served": False,
                "diagnostic": {
                    "stage": "release-snapshot",
                    "error_code": "verified-snapshot-unavailable",
                },
                "next_action": "Retry the normal MCP connection after the update completes; preserve the release transaction and packages.",
            }
        result = {"status": "current", "active": active}
    if result.get("diagnostic") and not check_only:
        print(json.dumps(result["diagnostic"]), file=sys.stderr)
    active = result.get("active")
    if not active:
        if check_only:
            return result
        return {
            "status": result["status"],
            "served": False,
            "next_action": "A verified compatible client is required before serving MCP.",
        }
    # ReleaseEngine.active rechecks every package hash before execution.
    if check_only or installation_check:
        active = releases.verified_active()
    elif event_hook:
        active = releases.verified_active(serving=True)
    runtime = releases.policy["runtime"]
    blocked = content_runtime_blocker(active, releases.policy)
    if blocked:
        return dict(result, **blocked)
    if check_only:
        return result
    if not installation_check and not event_hook:
        # The selected package is immutable. A later successful update affects
        # only future workers; this thread performs no native registration.
        try:
            threading.Thread(
                target=releases.startup_check,
                daemon=True,
                name="agentbrain-startup-check",
            ).start()
        except RuntimeError:
            print(
                json.dumps(
                    {
                        "stage": "release-check",
                        "error_code": "background-check-not-started",
                    }
                ),
                file=sys.stderr,
            )
    if releases.policy["capabilities"] in (
        CONTENT_CAPABILITIES,
        SSH_CONTENT_CAPABILITIES,
        HTTPS_CONTENT_CAPABILITIES,
    ):
        if not installation_check:
            from agent_brain_client.adapters.harnesses.codex.installation import inspect

            if not inspect(runtime["device_home"]).get("installed"):
                return {
                    "status": "content-native-install-pending",
                    "served": False,
                    "next_action": "Complete or recover the exact native installation transaction before mapping cloud content.",
                }
        command = [
            sys.executable,
            "-I",
            "-B",
            str(
                Path(active["package"])
                / "src/agent_brain_client/interfaces/mcp/content.py"
            ),
            "--client-version",
            active["version"],
        ]
        for name, value in runtime.items():
            if name not in LEGACY_PRESERVATION | DEPENDENCY_RUNTIME:
                command.extend(
                    [
                        "--" + name.replace("_", "-").replace("content-cache", "cache"),
                        value,
                    ]
                )
        if installation_check:
            command.append("--installation-check")
        if event_hook:
            if releases.policy["capabilities"] != HTTPS_CONTENT_CAPABILITIES:
                raise ValueError("Existing HTTPS device runtime required")
            command.extend(
                [
                    "--event-hook",
                    "--event-hook-policy",
                    str(event_hook[0]),
                    "--event-hook-policy-sha256",
                    event_hook[1],
                ]
            )
        if installation_check:
            return {
                "status": result["status"],
                "served": True,
                "exit_code": subprocess.run(command).returncode,
            }
        return serve_content(command, active, releases.policy, result["status"])
    if installation_check:
        return {"status": "content-runtime-review-required", "served": False}
    manifest = json.loads((Path(active["package"]) / "verified.json").read_bytes())[
        "manifest"
    ]
    if (
        hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        != active["manifest_sha256"]
    ):
        return {
            "status": "client-startup-retry-required",
            "served": False,
            "native_changed": False,
        }
    bridge = "src/agent_brain_client/interfaces/mcp/topics.py"
    command = [
        sys.executable,
        "-I",
        "-B",
        str(Path(active["package"]) / bridge),
        "--endpoint",
        runtime["endpoint"],
        "--token-file",
        runtime["token_file"],
        "--client-version",
        active["version"],
        "--client-platform",
        releases.policy["platform"],
    ]
    if "personal-memory-explicit" in releases.policy["capabilities"]:
        command.extend(["--data-mode", "personal"])
    if result["status"] not in ("current", "updated"):
        print(
            json.dumps(
                {
                    "client_update": result["status"],
                    "serving": "previous verified package",
                }
            ),
            file=sys.stderr,
        )
    return {
        "status": result["status"],
        "served": True,
        "exit_code": subprocess.run(command).returncode,
    }
