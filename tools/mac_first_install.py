"""Mac arm64 first installation using a reviewed self-contained app payload.

Connection files carry no credential. Native files change only through the
existing pinned installation transaction, after explicit server/scope review.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "client" if (ROOT / "client").is_dir() else ROOT
sys.path[:0] = [str(SOURCE / "adapters/codex"), str(SOURCE / "scripts")]
from brain_lock import locked
from client_release import (
    ClientReleases,
    HTTPS_CONTENT_CAPABILITIES,
    HTTPS_CONTENT_CLIENT_FILES,
    dependency_environment,
    tree_files,
)
from content_install import inspect_plan, prepare, private_json, safe, save
from https_content import (
    DeviceEnrollment,
    PairingHTTPS,
    endpoint_check,
    grant_check,
    install_once,
    native_store,
    profile_file,
)
from system_credentials import canonical, unique, owned

OFFICIAL_ORIGIN = "https://github.com/goodname427/agent-brain-client.git"
OFFICIAL_REF = "refs/heads/releases/personal-stable"


def sha(value):
    return hashlib.sha256(value).hexdigest()


def connection_file(path, expected=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("Connection file required")
    raw = path.read_bytes()
    if expected is not None and sha(raw) != expected:
        raise ValueError("Connection file drift")
    value = json.loads(raw, object_pairs_hook=unique)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "endpoint", "grant", "release"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
    ):
        raise ValueError("Credential-free connection scope required")
    endpoint_check(value["endpoint"])
    grant_check(value["grant"])
    if value["grant"]["platform"] != "darwin-arm64":
        raise ValueError("Mac arm64 grant required")
    release = value["release"]
    if (
        not isinstance(release, dict)
        or set(release) != {"origin", "ref", "anchor_commit"}
        or release["origin"] != OFFICIAL_ORIGIN
        or release["ref"] != OFFICIAL_REF
        or not re.fullmatch(r"[a-f0-9]{40}", release["anchor_commit"])
    ):
        raise ValueError("Existing official release authority required")
    return value, sha(raw)


def bundle_file(bundle):
    bundle = safe(bundle)
    path = bundle / "bundle-manifest.json"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 4194304:
        raise ValueError("Installer inventory required")
    raw = path.read_bytes()
    value = json.loads(raw, object_pairs_hook=unique)
    keys = {
        "schema",
        "platform",
        "files",
        "credential_helper_sha256",
        "credential_source_sha256",
        "publication",
        "signed",
        "notarized",
    }
    if (
        set(value) != keys
        or value["schema"] != 1
        or value["platform"] != "darwin-arm64"
    ):
        raise ValueError("Installer scope changed")
    actual = tree_files(bundle)
    actual.pop("bundle-manifest.json", None)
    if (
        actual != value["files"]
        or actual.get("credential-helper", {}).get("sha256")
        != value["credential_helper_sha256"]
        or actual.get("client/native/CredentialStore.swift", {}).get("sha256")
        != value["credential_source_sha256"]
    ):
        raise ValueError("Installer integrity changed")
    if not {
        "dependencies/python/bin/python3",
        "dependencies/git/bin/git",
        "tools/mac_first_install.py",
    } <= set(actual):
        raise ValueError("Self-contained installer required")
    return value, sha(raw)


def private_lease(state):
    state = safe(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    owned(state, directory=True)
    path = safe(state / "first-install.lock")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except FileExistsError:
        pass
    owned(path)
    return locked(path, timeout=0)


def payload_atomic(path, data, mode, staging):
    # An interrupted write leaves evidence outside the executable inventory.
    # Restart can fill the missing target without deleting any saved material.
    staging = safe(staging)
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    owned(staging, directory=True)
    descriptor, name = tempfile.mkstemp(prefix="payload-", dir=staging)
    try:
        with os.fdopen(descriptor, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def copy_payload(bundle, home):
    """Resume only missing identical payload files. Never overwrite drift."""
    manifest, digest = bundle_file(bundle)
    home = safe(home)
    state = safe(home / ".config/agent-brain-client/first-install")
    registration = home / ".config/agent-brain-client/codex-mcp.json"
    prior_receipt = home / ".config/agent-brain-client/content-install-receipt.json"
    if (
        registration.exists()
        or registration.is_symlink()
        or prior_receipt.exists()
        or prior_receipt.is_symlink()
    ) and not (state / "connection.json").is_file():
        raise ValueError("Existing installation requires its own migration plan")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with private_lease(state):
        payload = safe(state / "payload")
        payload.mkdir(mode=0o700, exist_ok=True)
        for name, row in manifest["files"].items():
            source, destination = Path(bundle) / name, payload / name
            # safe() rejects pre-existing symlink ancestors; the target itself
            # may be a reviewed internal link and is compared without following.
            safe(destination.parent).mkdir(parents=True, exist_ok=True, mode=0o700)
            if row.keys() == {"link"}:
                if destination.is_symlink():
                    if os.readlink(destination) != row["link"]:
                        raise ValueError("Payload link drift")
                elif destination.exists():
                    raise ValueError("Payload file preserved")
                else:
                    destination.symlink_to(row["link"])
            else:
                safe(destination)
                data = source.read_bytes()
                if sha(data) != row["sha256"]:
                    raise ValueError("Source changed during copy")
                if destination.exists():
                    if sha(destination.read_bytes()) != row["sha256"]:
                        raise ValueError("Existing payload drift")
                else:
                    payload_atomic(
                        destination,
                        data,
                        0o700 if row["executable"] else 0o600,
                        state / "payload-staging",
                    )
        pin = payload / "bundle-manifest.json"
        if pin.exists() and sha(pin.read_bytes()) != digest:
            raise ValueError("Installer version drift; preserve current transaction")
        if not pin.exists():
            payload_atomic(
                pin,
                (Path(bundle) / "bundle-manifest.json").read_bytes(),
                0o600,
                state / "payload-staging",
            )
        bundle_file(payload)
        return payload


def same_private(path, value):
    path = safe(path)
    if path.exists():
        if private_json(path) != value:
            raise ValueError("Prepared configuration drift")
    else:
        save(path, value)
    return sha(path.read_bytes())


def prepare_materials(
    bundle,
    home,
    native,
    connection,
    connection_sha,
    *,
    updater=None,
    runner=subprocess.run,
):
    """Ready means dependencies, helper, release health and native plan checked."""
    manifest, bundle_sha = bundle_file(bundle)
    home, native = safe(home), safe(native)
    state = safe(home / ".config/agent-brain-client/first-install")
    binding = {
        "schema": 1,
        "connection_sha256": connection_sha,
        "bundle_sha256": bundle_sha,
        "device_home": str(home),
        "codex_home": str(native),
    }
    registration = home / ".config/agent-brain-client/codex-mcp.json"
    prior_receipt = home / ".config/agent-brain-client/content-install-receipt.json"
    if (
        registration.exists()
        or registration.is_symlink()
        or prior_receipt.exists()
        or prior_receipt.is_symlink()
    ) and not (state / "connection.json").exists():
        raise ValueError("Existing installation preserved")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    same_private(state / "binding.json", binding)
    same_private(state / "connection.json", connection)
    dependencies = Path(bundle) / "dependencies"
    deps = {"schema": 1, "root": str(dependencies), "files": tree_files(dependencies)}
    deps_sha = same_private(state / "dependencies.json", deps)
    helper = Path(bundle) / "credential-helper"
    profile = {
        "schema": 1,
        "transport": "https-system-credential",
        "adapter": "codex",
        "platform": "darwin-arm64",
        "data_mode": "personal",
        "endpoint": connection["endpoint"],
        "grant": connection["grant"],
        "credential_helper": str(helper),
        "credential_helper_sha256": manifest["credential_helper_sha256"],
        "credential_source_sha256": manifest["credential_source_sha256"],
        "state_directory": str(home / ".config/agent-brain-client/https"),
    }
    profile_sha = same_private(state / "https-profile.json", profile)
    content = {
        "schema": 1,
        "adapter": "codex",
        "kinds": connection["grant"]["artifact_types"],
        "ids": connection["grant"]["ids"],
        "dependencies": {"commands": [], "python_modules": []},
    }
    content_sha = same_private(state / "content-policy.json", content)
    runtime = {
        "https_profile": str(state / "https-profile.json"),
        "https_profile_sha256": profile_sha,
        "credential_source_sha256": manifest["credential_source_sha256"],
        "content_policy": str(state / "content-policy.json"),
        "content_policy_sha256": content_sha,
        "device_home": str(home),
        "codex_home": str(native),
        "content_cache": str(state / "content-cache"),
        "dependency_policy": str(state / "dependencies.json"),
        "dependency_policy_sha256": deps_sha,
    }
    policy = {
        "schema": 1,
        "adapter": "codex",
        "platform": "darwin-arm64",
        **connection["release"],
        "channel": "stable",
        "core_api": 1,
        "state_schema": 1,
        "capabilities": HTTPS_CONTENT_CAPABILITIES,
        "file_prefixes": list(HTTPS_CONTENT_CLIENT_FILES),
        "repository": str(state / "repository"),
        "cache": str(state / "releases"),
        "runtime": runtime,
    }
    policy_sha = same_private(state / "release-policy.json", policy)
    environment = dependency_environment(policy)
    environment.update(HOME=str(home), LANG="en_US.UTF-8")
    # No credential access in dependency/helper preflight.
    checked = runner(
        [str(helper), "--self-check"], capture_output=True, timeout=30, env=environment
    )
    if checked.returncode or json.loads(checked.stdout) != {
        "credential_helper_self_check": "passed",
        "keychain_accessed": False,
        "ui_opened": False,
    }:
        raise ValueError("Helper health unconfirmed")
    plan_path = state / "install-plan.json"
    if plan_path.exists():
        plan = private_json(plan_path)
        plan_sha = sha(canonical(plan))
        receipt = home / ".config/agent-brain-client/content-install-receipt.json"
        if receipt.exists() and private_json(receipt).get("phase") in (
            "complete",
            "applying",
        ):
            return plan, plan_sha, binding
        inspect_plan(plan_path, plan_sha)
        prepared_at = int(time.time())
        ready = {
            "schema": 1,
            "status": "first-install-ready",
            "endpoint": connection["endpoint"],
            "grant": connection["grant"],
            "connection_sha256": connection_sha,
            "bundle_sha256": bundle_sha,
            "plan_sha256": plan_sha,
            "prepared_at": prepared_at,
            "valid_until": prepared_at + 600,
            "native_changed": False,
            "credential_accessed": False,
        }
        save(state / "ready.json", ready)
        return plan, plan_sha, binding
    repository = safe(state / "repository")
    if not (repository / ".git").exists():
        if repository.exists() and any(repository.iterdir()):
            raise ValueError("Unrecognized release checkout preserved")
        runner(
            [str(dependencies / "git/bin/git"), "init", "--", str(repository)],
            capture_output=True,
            timeout=30,
            env=environment,
            check=True,
        )
    remote = runner(
        [
            str(dependencies / "git/bin/git"),
            "-C",
            str(repository),
            "remote",
            "get-url",
            "origin",
        ],
        capture_output=True,
        timeout=30,
        env=environment,
    )
    if remote.returncode:
        runner(
            [
                str(dependencies / "git/bin/git"),
                "-C",
                str(repository),
                "remote",
                "add",
                "origin",
                connection["release"]["origin"],
            ],
            capture_output=True,
            timeout=30,
            env=environment,
            check=True,
        )
    elif remote.stdout.decode().strip() != connection["release"]["origin"]:
        raise ValueError("Release origin drift")
    # Use the existing updater, immutable packages, preflight, health and rollback.
    # Never activate an unpublished bundled candidate in place of a trusted ref.
    update = updater or (
        lambda: ClientReleases(state / "release-policy.json", policy_sha).update()
    )
    old = {k: os.environ.get(k) for k in environment}
    try:
        os.environ.update(environment)
        result = update()
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if result.get("status") not in ("current", "updated", "offline") or not result.get(
        "active"
    ):
        raise ValueError(
            "Reviewed compatible release unavailable; invitation not ready"
        )
    plan = prepare(
        home, Path(bundle) / "client", state / "release-policy.json", policy_sha
    )
    plan_sha = same_private(plan_path, plan)
    prepared_at = int(time.time())
    ready = {
        "schema": 1,
        "status": "first-install-ready",
        "endpoint": connection["endpoint"],
        "grant": connection["grant"],
        "connection_sha256": connection_sha,
        "bundle_sha256": bundle_sha,
        "plan_sha256": plan_sha,
        "prepared_at": prepared_at,
        "valid_until": prepared_at + 600,
        "native_changed": False,
        "credential_accessed": False,
    }
    save(state / "ready.json", ready)
    return plan, plan_sha, binding


def dialog(message, buttons=("Cancel", "Continue")):
    # Metadata only. Invitation and credential entry use the native secure helper.
    script = (
        "display dialog "
        + json.dumps(message)
        + ' with title "Agent Brain Install" buttons {'
        + ",".join(json.dumps(button) for button in buttons)
        + "} default button "
        + json.dumps(buttons[-1])
        + ' cancel button "Cancel"'
    )
    result = subprocess.run(
        ["/usr/bin/osascript", "-"],
        input=script.encode(),
        capture_output=True,
        timeout=180,
    )
    if result.returncode:
        return "Cancel"
    return next(
        (
            button
            for button in buttons
            if ("button returned:" + button).encode() in result.stdout
        ),
        "Cancel",
    )


def run(
    bundle,
    home,
    native,
    connection,
    connection_sha,
    *,
    confirm=dialog,
    updater=None,
    runner=subprocess.run,
    store=None,
    transport=None,
    secure_input=None,
    installer=None,
):
    state = safe(Path(home) / ".config/agent-brain-client/first-install")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with private_lease(state):
            description = (
                "Server: "
                + connection["endpoint"]
                + "\nActor: "
                + connection["grant"]["actor"]
                + "\nScope: "
                + json.dumps(connection["grant"]["ids"], sort_keys=True)
                + "\nRead and put Memory/Rule/Skill. No session/history collection.\nRelease: "
                + connection["release"]["origin"]
                + "\nAnchor: "
                + connection["release"]["anchor_commit"]
            )
            if confirm(description) != "Continue":
                return {
                    "status": "first-install-cancelled",
                    "installed": False,
                    "native_changed": False,
                }
            plan, plan_sha, _ = prepare_materials(
                bundle,
                Path(home),
                Path(native),
                connection,
                connection_sha,
                updater=updater,
                runner=runner,
            )
            profile = profile_file(
                state / "https-profile.json",
                private_json(state / "release-policy.json")["runtime"][
                    "https_profile_sha256"
                ],
            )
            if not (
                Path(home) / ".config/agent-brain-client/content-install-receipt.json"
            ).exists():
                selected = confirm(
                    "Dependencies, helper, compatible release and native plan are ready.\nOnly now request a short-lived invitation from your operator.\nReadiness file: "
                    + str(state / "ready.json")
                    + "\nContinue opens secure local invitation input.",
                    ("Cancel", "Back", "Continue"),
                )
                if selected != "Continue":
                    return {
                        "status": "first-install-ready"
                        if selected == "Back"
                        else "first-install-cancelled",
                        "installed": False,
                        "native_changed": False,
                    }
            credentials = (
                store
                if store is not None
                else native_store(
                    profile, sha((state / "https-profile.json").read_bytes())
                )
            )
            peer = (
                transport
                if transport is not None
                else PairingHTTPS(profile["endpoint"])
            )
            result = install_once(
                plan,
                plan_sha,
                store=credentials,
                transport=peer,
                secure_input=secure_input,
                installer=installer,
            )
            if result.get("status") == "pairing-reenrollment-review-required":
                enrollment = DeviceEnrollment(
                    profile,
                    sha((state / "https-profile.json").read_bytes()),
                    credentials,
                    peer,
                    None,
                )
                return enrollment.reset_terminal(credentials.backend.confirm_reset)
            return result
    except RuntimeError as error:
        if str(error) == "Another operation is running; retry shortly.":
            return {
                "status": "first-install-in-progress",
                "installed": False,
                "native_changed": False,
            }
        return {
            "status": "first-install-review-or-resume-required",
            "installed": False,
            "native_changed": False,
        }
    except Exception:
        return {
            "status": "first-install-review-or-resume-required",
            "installed": False,
            "native_changed": False,
            "next_action": "Preserve the prepared payload, configuration, enrollment and journals. Retry the same connection; no new invitation until readiness is confirmed.",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--connection", type=Path)
    parser.add_argument("--expected-connection-sha256")
    args = parser.parse_args()
    try:
        if sys.platform != "darwin" or platform.machine().lower() != "arm64":
            raise ValueError("Actual Mac arm64 required")
        if args.connection is None:
            chosen = subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    'POSIX path of (choose file with prompt "Choose your credential-free Agent Brain connection file")',
                ],
                capture_output=True,
                timeout=180,
            )
            if chosen.returncode:
                print(
                    json.dumps(
                        {"status": "first-install-cancelled", "installed": False}
                    )
                )
                return 2
            args.connection = Path(chosen.stdout.decode().strip())
        connection, connection_sha = connection_file(
            args.connection, args.expected_connection_sha256
        )
        home = Path.home().resolve()
        native = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).absolute()
        target = home / ".config/agent-brain-client/first-install/payload"
        if args.bundle != target:
            payload = copy_payload(args.bundle, home)
            # All later paths bind to the persistent payload, never Downloads or
            # the app volume. Secret UI has not opened and no network was used.
            result = subprocess.run(
                [
                    str(payload / "dependencies/python/bin/python3"),
                    "-I",
                    "-B",
                    str(payload / "tools/mac_first_install.py"),
                    "--bundle",
                    str(payload),
                    "--connection",
                    str(args.connection),
                    "--expected-connection-sha256",
                    connection_sha,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=900,
            )
            value = json.loads(result.stdout)
        else:
            value = run(args.bundle, home, native, connection, connection_sha)
        # Fixed result keys only. Never forward raw child stderr/errors/HTTP data.
        if value.get("status") not in ("first-install-cancelled", "pairing-cancelled"):
            dialog(
                "Status: "
                + value.get("status", "first-install-review-required")
                + "\n"
                + (
                    "Installation completed. New MCP processes will use the existing startup update checks."
                    if value.get("installed")
                    else "Prepared files and recovery records are preserved. Retry this same connection after resolving the reported condition."
                ),
                ("Cancel", "Close"),
            )
        print(json.dumps(value))
        return 0 if value.get("installed") else 2
    except Exception:
        print(
            json.dumps(
                {
                    "status": "first-install-review-or-resume-required",
                    "installed": False,
                    "native_changed": False,
                }
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
