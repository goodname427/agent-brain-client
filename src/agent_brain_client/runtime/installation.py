"""Shared Mac/Windows repository installer using the existing pairing/install transaction.

Connection files carry no credential. Native files change only through the
existing pinned installation transaction, after explicit server/scope review.
"""

from agent_brain_client.application.installation.enrollment import DeviceEnrollment

from agent_brain_client.runtime.dependencies import prepare_dependencies

import sys
from pathlib import Path


import json
import os
import re
import subprocess
import time
import secrets

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT
from agent_brain_client.adapters.platforms.filesystem import (
    native_platform,
    private_directory,
    WINDOWS_PLATFORMS,
)
from agent_brain_client.adapters.packages.client import (
    ClientReleases,
    HTTPS_CONTENT_CAPABILITIES,
    https_files,
    dependency_environment,
    CHECK_UPDATE_COMMAND,
    UPDATE_COMMAND,
)
from agent_brain_client.adapters.installation.installation import (
    inspect_plan,
    prepare,
)
from agent_brain_client.adapters.storage.private_files import private_json, safe, save
from agent_brain_client.runtime.enrollment import install_once
from agent_brain_client.domain.service import endpoint_check
from agent_brain_client.adapters.server.https import (
    PairingHTTPS,
    grant_check,
    native_store,
    profile_file,
    profile_metadata,
)
from agent_brain_client.adapters.platforms.credentials import canonical, unique, owned
from agent_brain_client.domain.contract import encode as plan_bytes
from agent_brain_client.adapters.packages.build import export
import tempfile

OFFICIAL_ORIGIN = "https://github.com/goodname427/agent-brain-client.git"
OFFICIAL_REF = "refs/heads/releases/personal-https-stable"


def connection_file(path, expected=None):
    from agent_brain_client.adapters.storage.install_state import sha

    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("Connection file required")
    raw = path.read_bytes()
    if expected is not None and sha(raw) != expected:
        raise ValueError("Connection file drift")
    value = json.loads(raw, object_pairs_hook=unique)
    connection_value(value)
    return value, sha(raw)


def connection_value(value):
    """Validate metadata only; this never discovers or creates an identity."""
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "endpoint", "grant", "release"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
    ):
        raise ValueError("Credential-free connection scope required")
    endpoint_check(value["endpoint"])
    grant_check(value["grant"])
    if value["grant"]["platform"] != native_platform():
        raise ValueError("Actual device platform grant required")
    release = value["release"]
    if (
        not isinstance(release, dict)
        or set(release) != {"origin", "ref", "anchor_commit"}
        or release["origin"] != OFFICIAL_ORIGIN
        or release["ref"]
        != (
            "refs/heads/releases/personal-https-windows-stable"
            if value["grant"]["platform"] in WINDOWS_PLATFORMS
            else OFFICIAL_REF
        )
        or not re.fullmatch(r"[a-f0-9]{40}", release["anchor_commit"])
    ):
        raise ValueError("Existing official release authority required")
    return value


class ConnectionRequired(ValueError):
    pass


def bootstrap_state_only(state):
    """Installer caches are preparation, never identity or release authority."""
    state = safe(state)
    if not state.exists():
        return True
    for item in state.iterdir():
        if item.name not in ("install-runtime", "installer-checkouts"):
            return False
        owned(item, directory=True)
    return True


def resolve_connection(home, native, path=None, expected=None):
    """Restore only the connection authenticated by an existing local binding.

    Saved connection files alone are not authority. A completed removal receipt
    or verified registration must still pin the profile, policy and bootstrap.
    Recovery stays in memory; it never exports helper paths or reads Keychain.
    """
    from agent_brain_client.adapters.storage.install_state import sha

    if path is not None:
        return connection_file(path, expected)
    if expected is not None:
        raise ValueError("An expected file digest requires an explicit file")
    from agent_brain_client.runtime.client_lifecycle import (
        read_removal,
        validate_uninstalled_state,
    )
    from agent_brain_client.adapters.installation.installation import (
        inspect,
        policy_file,
    )

    state = safe(Path(home) / ".config/agent-brain-client")
    registration = safe(state / "codex-mcp.json")
    removal = safe(state / "uninstall-receipt.json")
    if removal.exists() and read_removal(home)["phase"] in ("prepared", "applying"):
        raise ValueError("Unfinished removal requires its existing recovery")
    if registration.exists() and inspect(home).get("registration_verified"):
        metadata = private_json(owned(registration))
    elif removal.exists():
        owned(removal)
        journal = read_removal(home)
        if journal["phase"] != "complete":
            raise ValueError("Unfinished removal requires its existing recovery")
        resume_path = safe(state / "first-install/reinstall-current.json")
        if resume_path.exists():
            current = private_json(resume_path)
            if (
                set(current) != {"schema", "removed_plan_sha256", "plan", "plan_sha256"}
                or current["schema"] != 1
            ):
                raise ValueError("Reinstall recovery metadata changed")
            from agent_brain_client.adapters.storage.pinned import pinned

            resume_plan = pinned(current["plan"], current["plan_sha256"])
            if (
                resume_plan.get("device_home") != str(Path(home))
                or resume_plan.get("codex_home") != str(Path(native))
                or resume_plan.get("release_policy")
                != journal["registration"]["release_policy"]
                or resume_plan.get("release_policy_sha256")
                != journal["registration"]["release_policy_sha256"]
            ):
                raise ValueError("Reinstall recovery scope changed")
            removal_plan_sha = journal["registration"]["plan_sha256"]
            resumes_this_removal = current["removed_plan_sha256"] == removal_plan_sha
            is_completed_previous_reinstall = current["plan_sha256"] == removal_plan_sha
            if not resumes_this_removal and not is_completed_previous_reinstall:
                raise ValueError("Reinstall recovery metadata changed")
            if is_completed_previous_reinstall and not resumes_this_removal:
                # The saved plan is the installation just removed. Let the
                # existing reinstall_removed branch retire this completed marker
                # after user confirmation, then prepare the next transaction.
                validate_uninstalled_state(
                    home, journal, allow_unrelated_config_edits=True
                )
            else:
                install_receipt = safe(state / "content-install-receipt.json")
                resume_receipt = (
                    private_json(owned(install_receipt))
                    if install_receipt.exists()
                    else None
                )
                if (
                    resume_receipt is not None
                    and resume_receipt.get("phase") == "applying"
                ):
                    if resume_receipt.get("plan_sha256") != current["plan_sha256"]:
                        raise ValueError("Reinstall transaction receipt changed")
                elif resume_receipt is not None and resume_receipt.get("phase") not in (
                    "complete",
                    "rolled-back",
                ):
                    raise ValueError("Reinstall transaction phase requires review")
                validate_uninstalled_state(
                    home,
                    journal,
                    resume_plan=resume_plan,
                    resume_receipt=resume_receipt
                    if resume_receipt and resume_receipt.get("phase") == "applying"
                    else None,
                )
        else:
            validate_uninstalled_state(home, journal, allow_unrelated_config_edits=True)
        metadata = journal["registration"]
    else:
        if registration.exists() or not bootstrap_state_only(state):
            raise ValueError("Unverified installation materials preserved")
        raise ConnectionRequired("Trusted issuer delivery required")
    policy = policy_file(
        metadata["release_policy"],
        metadata["release_policy_sha256"],
        runtime_check=False,
    )
    runtime = policy["runtime"]
    owned(Path(metadata["release_policy"]))
    owned(Path(runtime["https_profile"]))
    profile = profile_file(runtime["https_profile"], runtime["https_profile_sha256"])
    value = connection_value(
        {
            "schema": 1,
            "endpoint": profile["endpoint"],
            "grant": profile["grant"],
            "release": {k: policy[k] for k in ("origin", "ref", "anchor_commit")},
        }
    )
    existing_connection(home, native, value, metadata)
    saved = safe(state / "first-install/connection.json")
    if saved.exists():
        value, digest = connection_file(owned(saved))
        existing_connection(home, native, value, metadata)
        return value, digest
    return value, sha(canonical(value))


def retire_ready(state):
    from agent_brain_client.adapters.storage.install_state import same_private
    from agent_brain_client.adapters.storage.install_state import sha

    ready = state / "ready.json"
    if ready.exists():
        previous = private_json(ready)
        same_private(
            state / ("prior-ready-" + sha(ready.read_bytes()) + ".json"), previous
        )
        ready.unlink()  # Only issuance metadata; enrollment and history stay intact.


def installation_id(state):
    from agent_brain_client.adapters.storage.install_state import same_private

    path = state / "installation-id.json"
    value = (
        private_json(path)
        if path.exists()
        else {"schema": 1, "id": secrets.token_hex(16)}
    )
    if (
        set(value) != {"schema", "id"}
        or value["schema"] != 1
        or not re.fullmatch(r"[a-f0-9]{32}", value["id"])
    ):
        raise ValueError("Installation instance drift")
    same_private(path, value)
    return value["id"]


def readiness(state, connection, connection_sha, materials_sha, plan_sha):
    prepared_at = int(time.time())
    value = {
        "schema": 2,
        "installation_id": installation_id(state),
        "status": "first-install-ready",
        "endpoint": connection["endpoint"],
        "grant": connection["grant"],
        "connection_sha256": connection_sha,
        "bundle_sha256": materials_sha,
        "plan_sha256": plan_sha,
        "prepared_at": prepared_at,
        "valid_until": prepared_at + 600,
        "native_changed": False,
        "credential_accessed": False,
    }
    save(state / "ready.json", value)


def prepare_materials(
    materials,
    home,
    native,
    connection,
    connection_sha,
    *,
    harness,
    updater=None,
    runner=subprocess.run,
):
    """Ready means dependencies, helper, release health and native plan checked."""
    from agent_brain_client.adapters.storage.install_state import same_private
    from agent_brain_client.adapters.storage.install_state import sha

    bundle_sha = sha(canonical(materials))
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
    private_directory(state)
    binding["installation_id"] = installation_id(state)
    binding_path = state / "binding.json"
    if binding_path.exists() and "installation_id" not in private_json(binding_path):
        legacy = dict(binding)
        legacy.pop("installation_id")
        if private_json(binding_path) != legacy:
            raise ValueError("Prepared binding drift")
        same_private(state / "prior-binding.json", legacy)
        save(binding_path, binding)  # Only exact known metadata; preserve preimage.
    same_private(binding_path, binding)
    same_private(state / "connection.json", connection)
    deps_sha = same_private(state / "dependencies.json", materials["dependencies"])
    if os.name == "nt":
        descriptor = {
            "kind": "windows-credential-manager",
            "source_sha256": materials["source_sha256"],
        }
    else:
        helper = Path(materials["credential_helper"])
        descriptor = {
            "kind": "mac-login-keychain",
            "helper": str(helper),
            "helper_sha256": materials["helper_sha256"],
            "source_sha256": materials["source_sha256"],
        }
    from agent_brain_client.adapters.storage.reinstallation import read_journal

    fresh = read_journal(home)
    profile = profile_metadata(
        connection["endpoint"],
        connection["grant"],
        descriptor,
        str(home / ".config/agent-brain-client/https"),
        **(
            {"instance_id": fresh["id"]}
            if fresh and fresh["phase"] in ("ready", "complete")
            else {}
        ),
    )
    profile_sha = same_private(state / "https-profile.json", profile)
    content = {
        "schema": 1,
        "adapter": "codex",
        "kinds": connection["grant"]["artifact_types"],
        "ids": connection["grant"]["ids"],
        "dependencies": {"commands": [], "python_modules": []},
    }
    if fresh and fresh["phase"] in ("ready", "complete"):
        from agent_brain_client.domain.content_policy import validate_policy

        content = fresh["content"]
        validate_policy(content, connection["grant"])
    content_sha = same_private(state / "content-policy.json", content)
    if fresh and fresh["phase"] in ("ready", "complete"):
        from agent_brain_client.runtime.fresh_installation import retained_installation

        from agent_brain_client.runtime.harness_content import content_host
        target_cache = home / ".config/agent-brain-client" / harness / "content-cache"
        retained_installation(home, Path(fresh["native"]), target_host=content_host(harness)).restore_mapping(target_cache, content)
    runtime = {
        "https_profile": str(state / "https-profile.json"),
        "https_profile_sha256": profile_sha,
        "credential_source_sha256": materials["source_sha256"],
        "content_policy": str(state / "content-policy.json"),
        "content_policy_sha256": content_sha,
        "device_home": str(home),
        "codex_home": str(native),
        "content_cache": str(home / ".config/agent-brain-client" / harness / "content-cache") if native == home / ".config/agent-brain-client/shared-native" or harness == "codebuddy" else str(state / "content-cache"),
        "dependency_policy": str(state / "dependencies.json"),
        "dependency_policy_sha256": deps_sha,
    }
    policy = {
        "schema": 1,
        "adapter": "codex",
        "platform": connection["grant"]["platform"],
        **connection["release"],
        "channel": "stable",
        "core_api": 1,
        "state_schema": 1,
        "capabilities": HTTPS_CONTENT_CAPABILITIES,
        "file_prefixes": list(https_files(connection["grant"]["platform"])),
        "repository": str(state / "repository"),
        "cache": str(state / "releases"),
        "runtime": runtime,
    }
    policy_sha = same_private(state / "release-policy.json", policy)
    environment = dependency_environment(policy)
    environment.update(HOME=str(home), LANG="en_US.UTF-8")

    def update_release():
        update = updater or (
            lambda: ClientReleases(state / "release-policy.json", policy_sha).update()
        )
        old = {key: os.environ.get(key) for key in environment}
        try:
            os.environ.update(environment)
            return update()
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    # No credential access in dependency/helper preflight.
    if os.name != "nt":
        checked = runner(
            [str(helper), "--self-check"],
            capture_output=True,
            timeout=30,
            env=environment,
        )
        if checked.returncode or json.loads(checked.stdout) != {
            "credential_helper_self_check": "passed",
            "keychain_accessed": False,
            "ui_opened": False,
        }:
            raise ValueError("Helper health unconfirmed")
    plan_path = state / "install-plan.json"
    removal = home / ".config/agent-brain-client/uninstall-receipt.json"
    from agent_brain_client.runtime.client_lifecycle import read_removal

    if removal.exists() and read_removal(home).get("phase") == "applying":
        raise ValueError("Resume uninstall with --action repair before reinstall")
    if plan_path.exists():
        plan = private_json(plan_path)
        plan_sha = sha(plan_bytes(plan))
        receipt = home / ".config/agent-brain-client/content-install-receipt.json"
        if receipt.exists() and private_json(receipt).get("phase") in (
            "complete",
            "applying",
        ):
            return plan, plan_sha, binding
        plan_sha = sha(plan_bytes(plan))
        inspect_plan(plan_path, plan_sha)
        readiness(state, connection, connection_sha, bundle_sha, plan_sha)
        return plan, plan_sha, binding
    repository = safe(state / "repository")
    if not (repository / ".git").exists():
        if repository.exists() and any(repository.iterdir()):
            raise ValueError("Unrecognized release checkout preserved")
        runner(
            [materials["dependencies"]["git"], "init", "--", str(repository)],
            capture_output=True,
            timeout=30,
            env=environment,
            check=True,
        )
    remote = runner(
        [
            materials["dependencies"]["git"],
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
                materials["dependencies"]["git"],
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
    result = update_release()
    if result.get("status") not in ("current", "updated", "offline") or not result.get(
        "active"
    ):
        raise ValueError(
            "Reviewed compatible release unavailable; invitation not ready"
        )
    package = state / "bootstrap"
    if not package.exists():
        staging = Path(tempfile.mkdtemp(prefix="bootstrap-", dir=state))
        export(
            SOURCE,
            staging / "package",
            https_content_candidate=True,
            target_platform=native_platform(),
        )
        os.rename(staging / "package", package)
    plan = prepare(home, package, state / "release-policy.json", policy_sha, harness=harness)
    plan_sha = same_private(plan_path, plan)
    readiness(state, connection, connection_sha, bundle_sha, plan_sha)
    return plan, plan_sha, binding


def confirm_target(message, buttons=("Cancel", "Continue")):
    """Terminal metadata only; secrets always use the existing secure helper."""
    print(message)
    print(" / ".join(buttons))
    try:
        chosen = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        return "Cancel"
    return chosen if chosen in buttons else "Cancel"


def existing_connection(home, native, connection, metadata):
    from agent_brain_client.adapters.installation.installation import context

    policy, _, _, configured_native = context(
        home,
        metadata["package"],
        metadata["release_policy"],
        metadata["release_policy_sha256"],
        harness=None,
    )
    runtime = policy["runtime"]
    profile = profile_file(runtime["https_profile"], runtime["https_profile_sha256"])
    if (
        configured_native != Path(native)
        or profile["endpoint"] != connection["endpoint"]
        or profile["grant"] != connection["grant"]
        or any(
            policy[k] != connection["release"][k]
            for k in ("origin", "ref", "anchor_commit")
        )
    ):
        raise ValueError("Existing identity or release authority differs; preserve it")
    return policy


def reinstall_removed(
    home,
    native,
    connection,
    *,
    harness,
    store=None,
    transport=None,
    secure_input=None,
    installer=None,
):
    """Reuse the original identity/config/cache; update only the fixed source entry."""
    from agent_brain_client.adapters.storage.install_state import same_private
    from agent_brain_client.adapters.storage.install_state import sha
    from agent_brain_client.runtime.client_lifecycle import (
        read_removal,
        validate_journal,
    )

    removed = read_removal(home)
    validate_journal(home, removed)
    metadata = removed["registration"]
    existing_connection(home, native, connection, metadata)
    state = safe(Path(home) / ".config/agent-brain-client/first-install")
    current_path = state / "reinstall-current.json"
    if current_path.exists():
        current = private_json(current_path)
        if (
            current.get("removed_plan_sha256") != metadata["plan_sha256"]
            and current.get("plan_sha256") == metadata["plan_sha256"]
        ):
            same_private(
                state
                / ("prior-reinstall-current-" + sha(canonical(current)) + ".json"),
                current,
            )
            current_path.unlink()  # Completed previous reinstall; never enrollment state.
    if current_path.exists():
        current = private_json(current_path)
        if (
            set(current) != {"schema", "removed_plan_sha256", "plan", "plan_sha256"}
            or current["schema"] != 1
            or current["removed_plan_sha256"] != metadata["plan_sha256"]
        ):
            raise ValueError("Reinstall attempt differs; preserve it")
        from agent_brain_client.adapters.storage.pinned import pinned

        plan = pinned(current["plan"], current["plan_sha256"])
        if (
            plan["device_home"] != str(home)
            or plan["codex_home"] != str(native)
            or plan["release_policy"] != metadata["release_policy"]
            or plan["release_policy_sha256"] != metadata["release_policy_sha256"]
        ):
            raise ValueError("Reinstall scope changed")
        result = install_once(
            plan,
            current["plan_sha256"],
            store=store,
            transport=transport,
            secure_input=lambda: None,
            installer=installer,
        )
        if result.get("installed"):
            removed["phase"] = "reinstalled"
            save(
                Path(home) / ".config/agent-brain-client/uninstall-receipt.json",
                removed,
            )
        return result
    from agent_brain_client.runtime.client_lifecycle import (
        validate_uninstalled_state,
    )

    validate_uninstalled_state(home, removed, allow_unrelated_config_edits=True)
    staging = Path(tempfile.mkdtemp(prefix="reinstall-bootstrap-", dir=state))
    manifest = export(
        SOURCE,
        staging / "package",
        https_content_candidate=True,
        target_platform=native_platform(),
    )
    package_sha = sha(canonical(manifest["files"]))
    package = state / ("bootstrap-" + package_sha)
    if not package.exists():
        os.rename(staging / "package", package)
    # Existing package is independently rehashed by prepare; never overwrite it.
    plan = prepare(
        home, package, metadata["release_policy"], metadata["release_policy_sha256"], harness=harness
    )
    plan_sha = sha(plan_bytes(plan))
    plan_path = state / ("reinstall-plan-" + plan_sha + ".json")
    same_private(plan_path, plan)
    same_private(
        current_path,
        {
            "schema": 1,
            "removed_plan_sha256": metadata["plan_sha256"],
            "plan": str(plan_path),
            "plan_sha256": plan_sha,
        },
    )
    result = install_once(
        plan,
        plan_sha,
        store=store,
        transport=transport,
        secure_input=lambda: None,
        installer=installer,
    )
    if result.get("installed"):
        removed["phase"] = "reinstalled"
        save(Path(home) / ".config/agent-brain-client/uninstall-receipt.json", removed)
    return result


def run(
    home,
    native,
    connection,
    connection_sha,
    *,
    harness,
    confirm=confirm_target,
    updater=None,
    runner=subprocess.run,
    dependency_preparer=prepare_dependencies,
    store=None,
    transport=None,
    secure_input=None,
    installer=None,
    access_mode=False,
):
    if harness not in ("codex", "codebuddy"):
        raise ValueError("Explicit installation Harness required")
    from agent_brain_client.adapters.storage.install_state import private_lease
    from agent_brain_client.adapters.storage.install_state import same_private
    from agent_brain_client.adapters.storage.install_state import sha

    state = safe(Path(home) / ".config/agent-brain-client/first-install")
    private_directory(state)
    try:
        with private_lease(state):
            description = (
                "Server: "
                + connection["endpoint"]
                + "\nActor: "
                + connection["grant"]["actor"]
                + "\nScope: "
                + json.dumps(connection["grant"]["ids"], sort_keys=True)
                + "\nMethods: read, put. Types: "
                + json.dumps(connection["grant"]["artifact_types"])
                + "\nNo session/history collection.\nRelease: "
                + connection["release"]["origin"]
                + "\nAnchor: "
                + connection["release"]["anchor_commit"]
            )
            if access_mode:
                print(description, file=sys.stderr)
            if not access_mode and confirm(description) != "Continue":
                return {
                    "status": "first-install-cancelled",
                    "installed": False,
                    "native_changed": False,
                }
            from agent_brain_client.runtime.client_lifecycle import (
                read_removal,
            )
            from agent_brain_client.adapters.installation.installation import inspect

            registration = Path(home) / ".config/agent-brain-client/codex-mcp.json"
            if (
                registration.exists()
                and inspect(home).get("status") == "content-native-client-disabled"
            ):
                existing_connection(
                    home, native, connection, private_json(registration)
                )
                return dict(
                    inspect(home),
                    next_action="Re-enable only Agent Brain in the host MCP settings and reload it to resume, or uninstall the paused service. No invitation is requested.",
                )
            if registration.exists() and inspect(home).get("installed"):
                existing_connection(
                    home, native, connection, private_json(registration)
                )
                removal = (
                    Path(home) / ".config/agent-brain-client/uninstall-receipt.json"
                )
                if removal.exists() and read_removal(home).get("phase") == "complete":
                    removed = read_removal(home)
                    removed["phase"] = "reinstalled"
                    save(removal, removed)
                return dict(
                    inspect(home),
                    update_check_performed=False,
                    next_action="Existing installation verified; repeating install does not check or apply updates. Run "
                    + CHECK_UPDATE_COMMAND
                    + " then "
                    + UPDATE_COMMAND
                    + " to use the original trusted channel.",
                )
            removal = Path(home) / ".config/agent-brain-client/uninstall-receipt.json"
            if removal.exists() and read_removal(home).get("phase") == "complete":
                return reinstall_removed(
                    Path(home),
                    Path(native),
                    connection,
                    store=store,
                    transport=transport,
                    secure_input=secure_input,
                    installer=installer,
                    harness=harness,
                )
            retire_ready(state)
            materials = dependency_preparer(Path(home), runner=runner)
            plan, plan_sha, _ = prepare_materials(
                materials,
                Path(home),
                Path(native),
                connection,
                connection_sha,
                harness=harness,
                updater=updater,
                runner=runner,
            )
            attempt = state / "pairing-attempt.json"
            resuming = attempt.exists()
            if resuming:
                retire_ready(state)
            if (
                not access_mode
                and not (
                    Path(home)
                    / ".config/agent-brain-client/content-install-receipt.json"
                ).exists()
            ):
                selected = confirm(
                    (
                        "Resume the saved pairing attempt. Do not request a new invitation."
                        if resuming
                        else "Dependencies, helper, compatible release and native plan are ready.\nFor your first attempt only, obtain the invitation using readiness: "
                        + str(state / "ready.json")
                    )
                    + "\nContinue uses existing recovery or opens the secure input once.",
                    ("Cancel", "Back", "Continue"),
                )
                if selected != "Continue":
                    return {
                        "status": (
                            "pairing-resume-required"
                            if resuming
                            else "first-install-ready"
                        )
                        if selected == "Back"
                        else "first-install-cancelled",
                        "installed": False,
                        "native_changed": False,
                    }
            same_private(attempt, {"schema": 1, "plan_sha256": plan_sha})
            result = install_once(
                plan,
                plan_sha,
                store=store,
                transport=transport,
                secure_input=secure_input,
                installer=installer,
            )
            removal = Path(home) / ".config/agent-brain-client/uninstall-receipt.json"
            if result.get("installed") and removal.exists():
                from agent_brain_client.runtime.client_lifecycle import (
                    read_removal,
                )

                removed = read_removal(Path(home))
                if removed.get("phase") == "complete":
                    removed["phase"] = "reinstalled"
                    save(removal, removed)
            retire_ready(state)
            if result.get("status") in ("pairing-cancelled", "pairing-invalid-input"):
                attempt.unlink()
            if (
                result.get("status") == "pairing-reenrollment-review-required"
                and access_mode
            ):
                return dict(
                    result,
                    status="access-device-recovery-required",
                    credential_record_preserved=True,
                    next_action="Preserve the revoked device identity and offline queue; arrange exact device recovery with the Server operator. Do not clear or replace credentials automatically.",
                )
            if result.get("status") == "pairing-reenrollment-review-required":
                profile_sha = private_json(state / "release-policy.json")["runtime"][
                    "https_profile_sha256"
                ]
                profile = profile_file(state / "https-profile.json", profile_sha)
                credentials = (
                    store if store is not None else native_store(profile, profile_sha)
                )
                peer = (
                    transport
                    if transport is not None
                    else PairingHTTPS(profile["endpoint"])
                )
                enrollment = DeviceEnrollment(
                    profile,
                    sha((state / "https-profile.json").read_bytes()),
                    credentials,
                    peer,
                    None,
                )
                reset = enrollment.reset_terminal(credentials.backend.confirm_reset)
                if reset.get("status") == "pairing-reenrollment-ready":
                    attempt.unlink()
                return reset
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
            "next_action": "Preserve the prepared dependencies, configuration, enrollment and journals. Retry the same connection; no new invitation until readiness is confirmed.",
        }


def finish_install(home, value, *, harness=None):
    from agent_brain_client.runtime.updates import manage_update
    from agent_brain_client.runtime.installation_completion import complete

    if os.name == "nt" and value.get("installed") is True:
        updated = manage_update(home, "update")
        value = dict(value, launcher_update=updated)
        if updated.get("status") not in (
            "client-update-current",
            "client-update-updated",
        ):
            return dict(
                value,
                installation_complete=False,
                next_action="Resume the public update entry to complete the trusted launcher handover before mapping or Hook setup.",
            )
    return complete(home, value, harness=harness)


def recover_windows_setup(home, native):
    if not native_platform().startswith("win32-"):
        return None
    from agent_brain_client.runtime.installation_completion import resume_setup

    return resume_setup(home, native)
