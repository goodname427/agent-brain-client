"""Mac arm64 repository installer using the existing pairing/install transaction.

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
import secrets

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT
sys.path[:0] = [str(SOURCE / name) for name in ("adapters/codex", "scripts", "tools")]
from brain_lock import locked
from client_release import (
    ClientReleases,
    HTTPS_CONTENT_CAPABILITIES,
    HTTPS_CONTENT_CLIENT_FILES,
    dependency_environment,
    CHECK_UPDATE_COMMAND,
    UPDATE_COMMAND,
    DEPENDENCY_RUNTIME,
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
    profile_metadata,
)
from system_credentials import canonical, unique, owned
from build_client import export
from build_credential_helper import build as build_helper
import shutil
import ssl
import tempfile

OFFICIAL_ORIGIN = "https://github.com/goodname427/agent-brain-client.git"
OFFICIAL_REF = "refs/heads/releases/personal-https-stable"


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
    return value


class ConnectionRequired(ValueError):
    pass


def resolve_connection(home, native, path=None, expected=None):
    """Restore only the connection authenticated by an existing local binding.

    Saved connection files alone are not authority. A completed removal receipt
    or verified registration must still pin the profile, policy and bootstrap.
    Recovery stays in memory; it never exports helper paths or reads Keychain.
    """
    if path is not None:
        return connection_file(path, expected)
    if expected is not None:
        raise ValueError("An expected file digest requires an explicit file")
    from client_lifecycle import read_removal, validate_journal
    from content_install import inspect, policy_file

    state = safe(Path(home) / '.config/agent-brain-client')
    registration = safe(state / 'codex-mcp.json')
    removal = safe(state / 'uninstall-receipt.json')
    if removal.exists() and read_removal(home)['phase'] in ('prepared', 'applying'):
        raise ValueError("Unfinished removal requires its existing recovery")
    if registration.exists() and inspect(home).get('registration_verified'):
        metadata = private_json(owned(registration))
    elif removal.exists():
        owned(removal)
        journal = read_removal(home)
        if journal['phase'] != 'complete':
            raise ValueError("Unfinished removal requires its existing recovery")
        validate_journal(home, journal)
        # An interrupted reinstall is checked by its existing pinned plan below.
        # Otherwise require every completed uninstall postimage before prompting.
        if not safe(state / 'first-install/reinstall-current.json').exists():
            from client_lifecycle import decode
            from content_install import raw, digest_or_none
            for name, after in journal['after'].items():
                if digest_or_none(raw(name, 1048576)) != digest_or_none(decode(after)):
                    raise ValueError("Uninstalled state changed")
        metadata = journal['registration']
    else:
        if registration.exists() or state.exists() and any(p.name != 'install-runtime' for p in state.iterdir()):
            raise ValueError("Unverified installation materials preserved")
        raise ConnectionRequired("Trusted issuer delivery required")
    policy = policy_file(metadata['release_policy'], metadata['release_policy_sha256'], runtime_check=False)
    runtime = policy['runtime']
    owned(Path(metadata['release_policy']))
    owned(Path(runtime['https_profile']))
    profile = profile_file(runtime['https_profile'], runtime['https_profile_sha256'])
    value = connection_value({'schema': 1, 'endpoint': profile['endpoint'],
                              'grant': profile['grant'],
                              'release': {k: policy[k] for k in ('origin', 'ref', 'anchor_commit')}})
    existing_connection(home, native, value, metadata)
    saved = safe(state / 'first-install/connection.json')
    if saved.exists():
        value, digest = connection_file(owned(saved))
        existing_connection(home, native, value, metadata)
        return value, digest
    return value, sha(canonical(value))


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


def same_private(path, value):
    path = safe(path)
    if path.exists():
        if private_json(path) != value:
            raise ValueError("Prepared configuration drift")
    else:
        save(path, value)
    return sha(path.read_bytes())


def retire_ready(state):
    ready = state / "ready.json"
    if ready.exists():
        previous = private_json(ready)
        same_private(state / ("prior-ready-" + sha(ready.read_bytes()) + ".json"), previous)
        ready.unlink()  # Only issuance metadata; enrollment and history stay intact.


def prepare_dependencies(home, *, runner=subprocess.run, helper_builder=build_helper):
    """Use checked local tools; compile the original helper once at a stable path."""
    if sys.platform != "darwin" or platform.machine().lower() != "arm64":
        raise ValueError("Reviewed Mac arm64 backend required")
    if sys.version_info < (3, 11) or not ssl.create_default_context().get_ca_certs():
        raise ValueError("Python 3.11+ and normal TLS roots required")
    python = Path(sys.executable).resolve()
    git_found = shutil.which("git")
    if git_found is None:
        raise ValueError("Git required; rerun the repository bootstrap")
    git = Path(git_found).resolve()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "en_US.UTF-8",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "0"}
    runner([str(git), "--version"], capture_output=True, timeout=30, env=env, check=True)
    core = Path(runner([str(git), "--exec-path"], capture_output=True,
                       timeout=30, env=env, check=True).stdout.decode().strip()).resolve()
    if not core.is_dir() or not (core / "git-remote-https").is_file():
        raise ValueError("Git HTTPS transport required")
    state = safe(Path(home) / ".config/agent-brain-client/first-install")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    owned(state, directory=True)
    helper = state / "credential-helper"
    pin = state / "helper.json"
    if helper.exists() or pin.exists():
        metadata = private_json(pin)
        if not helper.is_file() or helper.is_symlink():
            raise ValueError("Existing helper preserved; review incomplete preparation")
        if metadata != {"helper_sha256": sha(helper.read_bytes()),
                        "source_sha256": sha((SOURCE / "native/CredentialStore.swift").read_bytes())}:
            raise ValueError("Helper drift; preserve existing credential binding")
    else:
        built = helper_builder(helper)
        metadata = {"helper_sha256": built["helper_sha256"],
                    "source_sha256": built["source_sha256"]}
        same_private(pin, metadata)
    deps = {"schema": 2, "python": str(python), "git": str(git),
            "git_exec_path": str(core), "python_sha256": sha(python.read_bytes()),
            "git_sha256": sha(git.read_bytes())}
    same_private(state / "dependencies.json", deps)
    return dict(metadata, credential_helper=str(helper), dependencies=deps)


def installation_id(state):
    path = state / "installation-id.json"
    value = private_json(path) if path.exists() else {"schema": 1, "id": secrets.token_hex(16)}
    if set(value) != {"schema", "id"} or value["schema"] != 1 or not re.fullmatch(r"[a-f0-9]{32}", value["id"]):
        raise ValueError("Installation instance drift")
    same_private(path, value)
    return value["id"]


def readiness(state, connection, connection_sha, materials_sha, plan_sha):
    prepared_at = int(time.time())
    value = {"schema": 2, "installation_id": installation_id(state),
             "status": "first-install-ready", "endpoint": connection["endpoint"],
             "grant": connection["grant"], "connection_sha256": connection_sha,
             "bundle_sha256": materials_sha, "plan_sha256": plan_sha,
             "prepared_at": prepared_at, "valid_until": prepared_at + 600,
             "native_changed": False, "credential_accessed": False}
    save(state / "ready.json", value)


def prepare_materials(
    materials,
    home,
    native,
    connection,
    connection_sha,
    *,
    updater=None,
    runner=subprocess.run,
):
    """Ready means dependencies, helper, release health and native plan checked."""
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
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    helper = Path(materials["credential_helper"])
    profile = profile_metadata(connection["endpoint"], connection["grant"],
                               {"kind": "mac-login-keychain", "helper": str(helper),
                                "helper_sha256": materials["helper_sha256"],
                                "source_sha256": materials["source_sha256"]},
                               str(home / ".config/agent-brain-client/https"))
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
        "credential_source_sha256": materials["source_sha256"],
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
    removal = home / ".config/agent-brain-client/uninstall-receipt.json"
    from client_lifecycle import read_removal
    if removal.exists() and read_removal(home).get("phase") == "applying":
        raise ValueError("Resume uninstall with --action repair before reinstall")
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
    package = state / "bootstrap"
    if not package.exists():
        staging = Path(tempfile.mkdtemp(prefix="bootstrap-", dir=state))
        export(SOURCE, staging / "package", https_content_candidate=True)
        os.rename(staging / "package", package)
    plan = prepare(home, package, state / "release-policy.json", policy_sha)
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
    from content_install import context
    policy, _, _, configured_native = context(home, metadata['package'], metadata['release_policy'], metadata['release_policy_sha256'])
    runtime = policy['runtime']
    profile = profile_file(runtime['https_profile'], runtime['https_profile_sha256'])
    if configured_native != Path(native) or profile['endpoint'] != connection['endpoint'] or profile['grant'] != connection['grant'] or any(policy[k] != connection['release'][k] for k in ('origin', 'ref', 'anchor_commit')):
        raise ValueError('Existing identity or release authority differs; preserve it')
    return policy


def reinstall_removed(home, native, connection, *, store=None, transport=None, secure_input=None, installer=None):
    """Reuse the original identity/config/cache; update only the fixed source entry."""
    from client_lifecycle import read_removal, validate_journal, decode
    from content_install import raw, digest_or_none
    removed = read_removal(home)
    validate_journal(home, removed)
    metadata = removed['registration']
    existing_connection(home, native, connection, metadata)
    state = safe(Path(home) / '.config/agent-brain-client/first-install')
    current_path = state / 'reinstall-current.json'
    if current_path.exists():
        current = private_json(current_path)
        if current.get('removed_plan_sha256') != metadata['plan_sha256'] and current.get('plan_sha256') == metadata['plan_sha256']:
            same_private(state / ('prior-reinstall-current-' + sha(canonical(current)) + '.json'), current)
            current_path.unlink()  # Completed previous reinstall; never enrollment state.
    if current_path.exists():
        current = private_json(current_path)
        if set(current) != {'schema', 'removed_plan_sha256', 'plan', 'plan_sha256'} or current['schema'] != 1 or current['removed_plan_sha256'] != metadata['plan_sha256']:
            raise ValueError('Reinstall attempt differs; preserve it')
        from content_mcp import pinned
        plan = pinned(current['plan'], current['plan_sha256'])
        if plan['device_home'] != str(home) or plan['codex_home'] != str(native) or plan['release_policy'] != metadata['release_policy'] or plan['release_policy_sha256'] != metadata['release_policy_sha256']:
            raise ValueError('Reinstall scope changed')
        result = install_once(plan, current['plan_sha256'], store=store, transport=transport,
                              secure_input=lambda: None, installer=installer)
        if result.get('installed'):
            removed['phase'] = 'reinstalled'
            save(Path(home) / '.config/agent-brain-client/uninstall-receipt.json', removed)
        return result
    for name, value in removed['after'].items():
        if digest_or_none(raw(name, 1048576)) != digest_or_none(decode(value)):
            raise ValueError('Uninstalled state changed')
    staging = Path(tempfile.mkdtemp(prefix='reinstall-bootstrap-', dir=state))
    manifest = export(SOURCE, staging / 'package', https_content_candidate=True)
    package_sha = sha(canonical(manifest['files']))
    package = state / ('bootstrap-' + package_sha)
    if not package.exists():
        os.rename(staging / 'package', package)
    # Existing package is independently rehashed by prepare; never overwrite it.
    plan = prepare(home, package, metadata['release_policy'], metadata['release_policy_sha256'])
    plan_sha = sha(canonical(plan))
    plan_path = state / ('reinstall-plan-' + plan_sha + '.json')
    same_private(plan_path, plan)
    same_private(current_path, {'schema': 1, 'removed_plan_sha256': metadata['plan_sha256'],
                                'plan': str(plan_path), 'plan_sha256': plan_sha})
    result = install_once(plan, plan_sha, store=store, transport=transport,
                          secure_input=lambda: None, installer=installer)
    if result.get('installed'):
        removed['phase'] = 'reinstalled'
        save(Path(home) / '.config/agent-brain-client/uninstall-receipt.json', removed)
    return result


def run(
    home,
    native,
    connection,
    connection_sha,
    *,
    confirm=confirm_target,
    updater=None,
    runner=subprocess.run,
    dependency_preparer=prepare_dependencies,
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
                + "\nMethods: read, put. Types: "
                + json.dumps(connection["grant"]["artifact_types"])
                + "\nNo session/history collection.\nRelease: "
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
            from client_lifecycle import read_removal
            from content_install import inspect
            registration = Path(home) / '.config/agent-brain-client/codex-mcp.json'
            if registration.exists() and inspect(home).get('status')=='content-native-client-disabled':
                existing_connection(home,native,connection,private_json(registration))
                return dict(inspect(home),next_action='Re-enable only Agent Brain in the host MCP settings and reload it to resume, or uninstall the paused service. No invitation is requested.')
            if registration.exists() and inspect(home).get('installed'):
                existing_connection(home, native, connection, private_json(registration))
                removal = Path(home) / '.config/agent-brain-client/uninstall-receipt.json'
                if removal.exists() and read_removal(home).get('phase') == 'complete':
                    removed = read_removal(home)
                    removed['phase'] = 'reinstalled'
                    save(removal, removed)
                return dict(inspect(home), update_check_performed=False,
                            next_action='Existing installation verified; repeating install does not check or apply updates. Run '+CHECK_UPDATE_COMMAND+' then '+UPDATE_COMMAND+' to use the original trusted channel.')
            removal = Path(home) / '.config/agent-brain-client/uninstall-receipt.json'
            if removal.exists() and read_removal(home).get('phase') == 'complete':
                return reinstall_removed(Path(home), Path(native), connection, store=store,
                                         transport=transport, secure_input=secure_input, installer=installer)
            retire_ready(state)
            materials = dependency_preparer(Path(home), runner=runner)
            plan, plan_sha, _ = prepare_materials(
                materials,
                Path(home),
                Path(native),
                connection,
                connection_sha,
                updater=updater,
                runner=runner,
            )
            attempt = state / "pairing-attempt.json"
            resuming = attempt.exists()
            if resuming:
                retire_ready(state)
            if not (
                Path(home) / ".config/agent-brain-client/content-install-receipt.json"
            ).exists():
                selected = confirm(
                    ("Resume the saved pairing attempt. Do not request a new invitation." if resuming else
                     "Dependencies, helper, compatible release and native plan are ready.\nFor your first attempt only, obtain the invitation using readiness: " + str(state / "ready.json"))
                    + "\nContinue uses existing recovery or opens the secure input once.",
                    ("Cancel", "Back", "Continue"),
                )
                if selected != "Continue":
                    return {
                        "status": ("pairing-resume-required" if resuming else "first-install-ready")
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
                from client_lifecycle import read_removal
                removed = read_removal(Path(home))
                if removed.get("phase") == "complete":
                    removed["phase"] = "reinstalled"
                    save(removal, removed)
            retire_ready(state)
            if result.get("status") in ("pairing-cancelled", "pairing-invalid-input"):
                attempt.unlink()
            if result.get("status") == "pairing-reenrollment-review-required":
                profile_sha = private_json(state / "release-policy.json")["runtime"]["https_profile_sha256"]
                profile = profile_file(state / "https-profile.json", profile_sha)
                credentials = store if store is not None else native_store(profile, profile_sha)
                peer = transport if transport is not None else PairingHTTPS(profile["endpoint"])
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


def reexec_update_runtime(home, action, *, runner=subprocess.run):
    """Use the originally pinned Python/Git without changing their binding."""
    from content_install import inspect, registered_context
    if not inspect(home).get('registration_verified'):
        return None
    meta = private_json(safe(Path(home) / '.config/agent-brain-client/codex-mcp.json'))
    policy, mcp, _, native = registered_context(home, meta)
    if not DEPENDENCY_RUNTIME <= set(policy['runtime']):
        return None
    environment = dependency_environment(policy, runtime_check=False)
    if Path(mcp['command']).resolve() == Path(sys.executable).resolve():
        return None
    environment = dict(environment, HOME=str(home), CODEX_HOME=str(native), LANG='en_US.UTF-8')
    return runner([mcp['command'], '-I', '-B', str(ROOT/'tools/mac_first_install.py'),
                   '--action', action], env=environment, stdin=subprocess.DEVNULL).returncode


def manage_update(home, action, *, channel_factory=None):
    """Reuse the registered updater; no pairing, native rewrite or worker restart.

    Discovery may cache verified candidates, but never changes the active pointer.
    The CLI cannot observe host worker versions. MCP responses report their own.
    """
    from content_install import inspect, registered_context
    result = {
        'operation': action, 'status': 'client-update-review-required',
        'active_package_version': None, 'active_package_commit': None,
        'running_worker_version': None, 'running_worker_commit': None,
        'latest_channel_version': None, 'latest_channel_commit': None,
        'update_available': None, 'reload_required': None,
        'channel_checked': False, 'active_package_changed': False,
        'native_changed': False, 'device_credentials_accessed': False,
        'next_action': 'Preserve the existing installation. Verify its registration, policy and bootstrap; do not replace trust metadata or re-pair.',
    }
    try:
        if action not in ('check-update', 'update'):
            raise ValueError('Known maintenance action required')
        state = inspect(home)
        if not state.get('registration_verified') or state.get('installation_state') not in ('verified', 'verified-disabled'):
            return dict(result, reason=state.get('reason', 'verified-installation-required'))
        meta = private_json(safe(Path(home) / '.config/agent-brain-client/codex-mcp.json'))
        policy, _, _, _ = registered_context(home, meta)
        if policy['capabilities'] != HTTPS_CONTENT_CAPABILITIES:
            return dict(result, reason='existing-https-installation-required')
        environment = dependency_environment(policy)
        previous = dict(os.environ)
        try:
            if environment is not None:
                os.environ.clear()
                os.environ.update(environment, HOME=str(home), LANG='en_US.UTF-8')
            channel = (channel_factory or ClientReleases)(meta['release_policy'], meta['release_policy_sha256'])
            before = channel.verified_active()
            if before is None:
                return dict(result, reason='verified-active-package-required')
            result.update(active_package_version=before['version'], active_package_commit=before.get('commit'))
            if action == 'check-update':
                discovered = channel.discover('codex', policy['platform'], before['version'])
                rows = discovered.get('releases', [])
                if discovered.get('status') not in ('current', 'compatible-update') or len(rows) != 1:
                    return dict(result, status='client-update-unavailable' if discovered.get('status') == 'release-unavailable' else 'client-update-review-required',
                                next_action='The trusted channel was not confirmed. Keep the current package and retry '+CHECK_UPDATE_COMMAND+'; do not call the local version the latest.')
                row = rows[0]
                latest_version, latest_commit = row['manifest']['version'], row['commit']
                available = before.get('commit') != latest_commit
                if channel.verified_active() != before:
                    return dict(result, reason='active-package-changed-during-check')
                result.update(status='client-update-available' if available else 'client-update-current',
                              latest_channel_version=latest_version, latest_channel_commit=latest_commit,
                              update_available=available, channel_checked=True,
                              next_action=('Run '+UPDATE_COMMAND+' to apply this trusted candidate. ' if available else 'The local active package matches the trusted channel. ')+
                                          'The CLI cannot observe the running worker. Confirm MCP client_runtime; a normal host reload starts a new worker when needed.')
                return result
            updated = channel.update()
            after = channel.verified_active()
            if updated.get('status') not in ('current', 'updated') or after is None:
                if after is not None:
                    changed = after != before
                    result.update(active_package_version=after['version'], active_package_commit=after.get('commit'),
                                  active_package_changed=changed, reload_required=True if changed else None)
                else:
                    result.update(active_package_version=None, active_package_commit=None,
                                  active_package_changed=None, reload_required=None)
                return dict(result, status='client-update-unavailable' if updated.get('status') == 'offline' else 'client-update-review-required',
                            next_action='Update was not confirmed. '+('A verified active package is retained or restored. ' if after is not None else 'No active package was verified; preserve recovery material. ')+
                                        'Retry the same public update entry after reviewing the installation/channel state. Do not repeat pairing or import.')
            changed = after != before
            result.update(status='client-update-updated' if changed else 'client-update-current',
                          active_package_version=after['version'], active_package_commit=after.get('commit'),
                          latest_channel_version=after['version'], latest_channel_commit=after.get('commit'),
                          update_available=False, channel_checked=True, active_package_changed=changed,
                          reload_required=True if changed else None,
                          next_action='The verified local package is ready. Normally reload only Agent Brain through the host so a new MCP worker loads it; do not kill in-flight work. Confirm client_runtime.running_worker_version matches latest_channel_version, then continue Onboard with fresh results. This command does not import real memories.')
            return result
        finally:
            os.environ.clear()
            os.environ.update(previous)
    except Exception:
        return result


def lifecycle_exit(action, value):
    """Exit success denotes the requested action, never merely an old installation."""
    status=value.get('status')
    if action == 'check-update' and status in ('client-update-available', 'client-update-current') and value.get('channel_checked') is True:
        return 0
    if action == 'update' and status in ('client-update-updated', 'client-update-current') and value.get('channel_checked') is True:
        return 0
    if action in ('status','repair','uninstall') and status in ('client-uninstalled','client-not-installed'):
        return 0
    if action in ('status','repair') and status=='content-native-client-disabled' and value.get('registration_verified') is True:
        return 0
    if action in ('status','repair','install') and status=='content-native-client-installed' and value.get('installed') is True:
        return 0
    return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("install", "status", "repair", "uninstall", "check-update", "update"), default="install")
    parser.add_argument("--connection", type=Path)
    parser.add_argument("--expected-connection-sha256")
    args = parser.parse_args()
    try:
        if sys.platform != "darwin" or platform.machine().lower() != "arm64":
            raise ValueError("Actual Mac arm64 required")
        home = Path.home().resolve()
        if args.action in ('check-update', 'update'):
            if args.connection is not None or args.expected_connection_sha256 is not None:
                value = {'status': 'client-update-review-required', 'reason': 'existing-policy-only',
                         'native_changed': False, 'device_credentials_accessed': False,
                         'next_action': 'Update management reuses the verified registration; do not pass a replacement connection or invitation.'}
            else:
                try:
                    delegated = reexec_update_runtime(home, args.action)
                    if delegated is not None:
                        return delegated
                    value = manage_update(home, args.action)
                except Exception:
                    value = {'operation': args.action, 'status': 'client-update-review-required',
                             'reason': 'existing-runtime-or-registration-unverified',
                             'channel_checked': False, 'native_changed': False,
                             'device_credentials_accessed': False,
                             'next_action': 'Restore the verified original runtime/registration; do not prepare new dependencies, replace bindings or re-pair.'}
            print(json.dumps(value))
            return lifecycle_exit(args.action, value)
        if args.action != "install":
            from client_lifecycle import status, repair, uninstall
            try:
                value = uninstall(home, confirm_target) if args.action == "uninstall" else repair(home) if args.action == "repair" else status(home)
            except RuntimeError:
                value = {"status": "client-in-use", "native_changed": False,
                         "next_action": "Disable only agent_brain_server in the host Settings > MCP servers, save and use the host Restart/reload. Wait for this MCP worker to exit before rerunning. If that service cannot be paused or the worker remains active, stop and report the blocker. Preserve other MCP settings and persistent locks."}
            except Exception:
                partial=False
                if args.action in ('uninstall','repair'):
                    from client_lifecycle import journal_path,read_removal
                    try:
                        partial=journal_path(home).exists() and read_removal(home).get('phase')=='applying'
                    except Exception:
                        partial=True
                value = {"status": "uninstall-recovery-review-required" if partial else "client-ownership-review-required", "native_changed": None if partial else False,
                         "next_action": "Preserve local files and recovery receipts; do not delete data or reset Keychain. Use status, then repair for interrupted uninstall."}
            print(json.dumps(value))
            return lifecycle_exit(args.action,value)
        native = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))).absolute()
        try:
            connection, connection_sha = resolve_connection(
                home, native, args.connection, args.expected_connection_sha256
            )
        except ConnectionRequired:
            print(json.dumps({"status": "first-install-connection-required", "installed": False,
                              "native_changed": False, "credential_accessed": False,
                              "next_action": "Obtain connection.json and its SHA-256 through your service issuer's trusted delivery channel. The issuer uses Server tools/export_connection.py; then run this script with --connection and --expected-connection-sha256. See docs/FIRST-INSTALL-MAC.md. No invitation is needed until ready.json exists."}))
            return 2
        except Exception:
            print(json.dumps({"status": "connection-file-review-required" if args.connection else "connection-recovery-review-required",
                              "installed": False, "native_changed": False, "credential_accessed": False,
                              "next_action": "Preserve connection metadata, original profile/policy and recovery receipts. Review the original binding; do not reset identity, replace digests or use an example connection."}))
            return 2
        value = run(home, native, connection, connection_sha)
        from content_contract import installation_follow_up
        value = installation_follow_up(value)
        print(json.dumps(value))
        return lifecycle_exit('install',value)
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
