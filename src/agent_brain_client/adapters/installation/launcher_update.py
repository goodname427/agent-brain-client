"""Update the owned fixed launcher using an exact recoverable transaction."""

import json
import os
from pathlib import Path
import subprocess
from agent_brain_client.adapters.packages.client import (
    ClientReleases,
    HTTPS_CONTENT_CAPABILITIES,
    MCP_LAUNCHER_ENTRY,
    UpdateBlocked,
    fixed_launcher_files,
    launcher_package_files,
    managed_worker_abi,
)
from agent_brain_client.adapters.storage.private_files import private_json, safe
import agent_brain_client.adapters.storage.private_files as private_files
from agent_brain_client.adapters.platforms.credentials import canonical


def completed_reinstall_registration(home, current, meta):
    """Recognize only the current install or its exact transaction predecessor."""
    from agent_brain_client.adapters.storage.install_state import sha

    if current["plan_sha256"] == meta["plan_sha256"]:
        return meta
    import base64
    import agent_brain_client.adapters.installation.installation as ci

    registration = safe(Path(home) / ".config/agent-brain-client/codex-mcp.json")
    receipt = private_json(registration.parent / "content-install-receipt.json")
    registered = private_files.raw(registration)
    row = receipt.get("before", {}).get(str(registration), {})
    if (
        receipt.get("schema") != 1
        or receipt.get("phase") != "complete"
        or receipt.get("plan_sha256") != meta["plan_sha256"]
        or receipt.get("after", {}).get(str(registration)) != sha(registered)
        or json.loads(registered, object_pairs_hook=ci.pairs) != meta
        or set(row) != {"data", "sha", "mode"}
        or not isinstance(row["data"], str)
    ):
        raise ValueError("Unconfirmed reinstall predecessor preserved")
    previous_raw = base64.b64decode(row["data"], validate=True)
    previous = json.loads(previous_raw, object_pairs_hook=ci.pairs)
    if (
        sha(previous_raw) != row["sha"]
        or not isinstance(previous, dict)
        or previous.get("schema") != 1
        or previous.get("content_candidate") is not True
        or previous.get("plan_sha256") != current["plan_sha256"]
        or previous.get("native_mcp_name") != meta["native_mcp_name"]
        or previous.get("platform") != meta["platform"]
    ):
        raise ValueError("Unconfirmed reinstall predecessor preserved")
    return previous


def retire_completed_reinstall(home, meta):
    """Archive a proven completed reinstall before replacing its registration."""
    from agent_brain_client.adapters.storage.install_state import same_private
    from agent_brain_client.adapters.storage.install_state import sha
    import agent_brain_client.adapters.installation.installation as ci
    from agent_brain_client.adapters.storage.pinned import pinned

    state = safe(Path(home) / ".config/agent-brain-client/first-install")
    current_path = safe(state / "reinstall-current.json")
    if not current_path.exists():
        return
    current = private_json(current_path)
    if (
        set(current) != {"schema", "removed_plan_sha256", "plan", "plan_sha256"}
        or current["schema"] != 1
    ):
        raise ValueError("Unconfirmed reinstall marker preserved")
    binding = completed_reinstall_registration(home, current, meta)
    plan = pinned(current["plan"], current["plan_sha256"])
    ci.validate_install_scope(plan)
    if (
        plan["device_home"] != str(home)
        or plan["package"] != binding["package"]
        or plan["package_sha256"] != binding["package_sha256"]
        or plan["release_policy"] != binding["release_policy"]
        or plan["release_policy_sha256"] != binding["release_policy_sha256"]
        or not ci.inspect(home).get("registration_verified")
    ):
        raise ValueError("Completed reinstall binding changed")
    same_private(
        state / ("prior-reinstall-current-" + sha(canonical(current)) + ".json"),
        current,
    )
    if private_json(current_path) != current:
        raise ValueError("Completed reinstall marker changed")
    current_path.unlink()


def can_retain_launcher(policy, installed, registered, candidate, desired, before):
    """Ignore only the verified worker-package path component of the binding."""
    if policy["capabilities"] != HTTPS_CONTENT_CAPABILITIES or before is None:
        return False
    installed, candidate = Path(installed), Path(candidate)
    old_entry = str(installed / MCP_LAUNCHER_ENTRY)
    new_entry = str(candidate / MCP_LAUNCHER_ENTRY)
    try:
        if registered["args"].count(old_entry) != 1 or desired["args"].count(new_entry) != 1:
            return False
        if registered != dict(desired, args=[
            old_entry if arg == new_entry else arg for arg in desired["args"]
        ]):
            return False
        old_files = launcher_package_files(installed)
        target_files = launcher_package_files(candidate)
        previous = Path(before["package"])
        previous_files = launcher_package_files(previous)
        fixed = fixed_launcher_files(old_files)
        if (
            MCP_LAUNCHER_ENTRY not in fixed
            or fixed != fixed_launcher_files(target_files)
            or fixed != fixed_launcher_files(previous_files)
        ):
            return False
        installed_abi = managed_worker_abi(installed, old_files)
        return (
            installed_abi is not None
            and installed_abi == managed_worker_abi(previous, previous_files)
            and installed_abi == managed_worker_abi(candidate, target_files)
        )
    except (OSError, ValueError, KeyError, TypeError, UpdateBlocked):
        return False


def refresh_launcher(
    home, *, channel_factory=None, channel=None, before=None, meta=None
):
    import agent_brain_client.adapters.installation.installation as ci
    from agent_brain_client.adapters.storage.host_files import HostConfigChanged

    for attempt in range(3):
        try:
            return apply_launcher_update(
                home,
                channel_factory=channel_factory,
                channel=channel,
                before=before,
                meta=meta,
            )
        except (HostConfigChanged, ci.InstallBlocked) as error:
            # These errors occur before journaling or writing host files. Later
            # failures return a recovery result and never enter this retry.
            if not isinstance(error, HostConfigChanged) and str(error) not in (
                "Install preimage or inputs changed",
                "Config rendering differs from approved plan",
            ):
                raise
            if attempt == 2:
                raise HostConfigChanged(
                    "config.toml is still being edited; retry update after the editor finishes"
                ) from None


def apply_launcher_update(
    home, *, channel_factory=None, channel=None, before=None, meta=None
):
    """Repair a stale fixed launcher on either platform through a verified transaction.

    The exact active checkpoint, policy, target package and native preimages are
    persisted before handover. No credentials, policy expansion or worker kill.
    """
    from agent_brain_client.adapters.storage.install_state import sha
    import agent_brain_client.adapters.installation.installation as ci

    marker = safe(Path(home) / ".config/agent-brain-client/launcher-handover.json")
    if marker.exists():
        saved = private_files.private_json(marker)
        if (
            set(saved) != {"schema", "plan", "active", "target_commit"}
            or saved["schema"] != 1
        ):
            raise ValueError("Launcher journal changed")
        plan = saved["plan"]
        ci.validate_install_scope(plan)
        if plan["device_home"] != str(home) or not plan["replace_existing"]:
            raise ValueError("Launcher journal binding changed")
        channel = (channel_factory or ClientReleases)(
            plan["release_policy"], plan["release_policy_sha256"]
        )
        before = saved["active"]
    else:
        discovered = channel.discover(
            "codex", channel.engine.policy["platform"], before["version"]
        )
        rows = discovered.get("releases", [])
        if (
            discovered.get("status") not in ("current", "compatible-update")
            or len(rows) != 1
        ):
            raise ValueError("Trusted launcher candidate unavailable")
        row = rows[0]
        candidate = safe(
            Path(channel.engine.local)
            / "packages"
            / (row["manifest"]["version"] + "-" + row["commit"])
        )
        ci.package_check(candidate, channel.engine.policy)
        _, registered, _, _ = ci.registered_context(home, meta)
        if "--harness" not in registered["args"]:
            raise ValueError("Reinstall with an explicit Harness to refresh retained legacy registration")
        plan = ci.prepare(
            home,
            candidate,
            meta["release_policy"],
            meta["release_policy_sha256"],
            replace_existing=True,
            harness=registered["args"][registered["args"].index("--harness") + 1],
        )
        _, registered, _, _ = ci.registered_context(home, meta)
        if str(candidate) == meta["package"] and plan["mcp"] == registered:
            return False
        if can_retain_launcher(
            channel.engine.policy, meta["package"], registered, candidate, plan["mcp"], before,
        ):
            return False
        saved = {
            "schema": 1,
            "plan": plan,
            "active": before,
            "target_commit": row["commit"],
        }
        # Confirm offline compatibility before making even a pending marker.
        probe_launcher(plan, before)
        retire_completed_reinstall(home, meta)
        private_files.save(marker, saved)
    plan = saved["plan"]
    # An editor may have added tools since the updater saved this plan. Refresh
    # only config preimages; prepare authenticates our own service independently
    # of every unrelated TOML field. Interrupted writes use exact recovery first.
    refreshed = ci.rebase_update_plan(plan)
    if refreshed != plan:
        saved = dict(saved, plan=refreshed)
        private_files.save(marker, saved)
        plan = refreshed
    target = safe(plan["package"])
    if target.parent != safe(Path(channel.engine.local) / "packages"):
        raise ValueError("Launcher package outside trusted cache")
    ci.package_check(target, channel.engine.policy)
    verified = json.loads(private_files.raw(target / "verified.json"))
    if verified["commit"] != saved["target_commit"]:
        raise ValueError("Launcher candidate changed")
    active = channel.verified_active()
    candidate_active = {
        "version": verified["manifest"]["version"],
        "commit": verified["commit"],
        "package": str(target),
        "manifest_sha256": sha(
            json.dumps(verified["manifest"], sort_keys=True).encode()
        ),
    }
    if active not in (before, candidate_active):
        raise ValueError("Launcher active checkpoint changed")
    pin = sha(ci.contract.encode(plan))
    receipt = private_files.private_json(
        Path(home) / ".config/agent-brain-client/content-install-receipt.json"
    )
    metadata = private_files.private_json(
        Path(home) / ".config/agent-brain-client/codex-mcp.json"
    )
    complete = (
        receipt.get("phase") == "complete"
        and receipt.get("plan_sha256") == pin
        and metadata.get("plan_sha256") == pin
    )
    result = (
        {"status": "content-native-client-installed"}
        if complete
        else ci.apply(plan, pin, check=lambda p: probe_launcher(p, active))
    )
    if result.get("status") == "content-install-rolled-back":
        marker.unlink()
        raise ValueError("Launcher transaction rolled back")
    inspected = ci.inspect(home)
    if not inspected.get("registration_verified") or inspected.get(
        "installation_state"
    ) not in ("verified", "verified-disabled"):
        if result.get("status") == "content-install-rolled-back":
            marker.unlink()
        raise ValueError("Launcher transaction unconfirmed; preserve journal")
    metadata = private_files.private_json(
        Path(home) / ".config/agent-brain-client/codex-mcp.json"
    )
    if metadata["package"] != str(target) or metadata["plan_sha256"] != pin:
        raise ValueError("Launcher registration target changed")
    if private_files.private_json(marker) != saved:
        raise ValueError("Launcher journal changed")
    marker.unlink()
    return True


def probe_launcher(plan, active):
    environment = dict(
        plan["mcp"].get("env", {}),
        HOME=plan["device_home"],
        CODEX_HOME=plan["codex_home"],
        LANG="en_US.UTF-8",
    )
    if os.name == "nt":
        environment.update(
            SystemRoot=os.environ["SystemRoot"], USERPROFILE=plan["device_home"]
        )
    run = subprocess.run(
        [plan["mcp"]["command"], *plan["mcp"]["args"], "--bootstrap-check"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        env=environment,
    )
    if run.returncode or len(run.stdout) > 16384:
        raise ValueError("Launcher offline compatibility unconfirmed")
    value = json.loads(run.stdout)
    if (
        value.get("status") != "client-bootstrap-verified"
        or value.get("active_package_version") != active["version"]
        or value.get("active_package_commit") != active.get("commit")
        or value.get("active_package") != active.get("package")
        or value.get("native_changed") is not False
        or value.get("network_used") is not False
        or value.get("device_credentials_accessed") is not False
    ):
        raise ValueError("Launcher checkpoint differs")


# This portion runs in isolated Python before importing any release code.
_BOOTSTRAP = '''
import hashlib
import pathlib
import subprocess
import sys

try:
    root = pathlib.Path(ANCHOR)
    src = root / "src"
    if root.resolve(strict=True) != root:
        raise ValueError("Linked launcher anchor")
    paths = list(src.rglob("*"))
    if (any(path.is_symlink() for path in paths)
            or {path.relative_to(root).as_posix() for path in paths if not path.is_dir()} != PINS.keys()):
        raise ValueError("Launcher anchor membership changed")
    for name, expected in PINS.items():
        file = root / name
        if (file.resolve(strict=True) != file
                or sys.platform != "win32" and file.stat().st_nlink != 1
                or hashlib.sha256(file.read_bytes()).hexdigest() != expected):
            raise ValueError("Launcher anchor changed")
    sys.path.insert(0, str(src))
    from agent_brain_client.adapters.packages.client import ClientReleases
    from agent_brain_client.adapters.storage.registration import registration

    saved, _ = registration(HOME)
    if (saved["release_policy"], saved["release_policy_sha256"]) != POLICY:
        raise ValueError("Hook enrollment changed")
    channel = ClientReleases(*POLICY)
    if channel.policy["runtime"]["device_home"] != HOME:
        raise ValueError("Hook release identity changed")
    active = channel.verified_active(serving=True)
    if active is None:
        raise ValueError("Verified serving Hook release required")
    package = pathlib.Path(active["package"])
    entry = package / "src/agent_brain_client/runtime/host_binding.py"
    if not entry.is_file() or b"def run_native_hook(" not in entry.read_bytes():
        raise ValueError("Verified Hook entry required")
    code = (
        "import sys; sys.path.insert(0,sys.argv.pop(1)); "
        "from agent_brain_client.runtime.host_binding import run_native_hook; "
        "sys.exit(run_native_hook(*sys.argv[1:]))"
    )
    status = subprocess.run(
        [sys.executable, "-X", "utf8", "-I", "-B", "-c", code, str(package / "src"),
         HOME, *sys.argv[1:]], check=False,
    ).returncode
except Exception as error:
    print("{}", flush=True)
    print("agent-brain Hook launch unavailable: " + type(error).__name__, file=sys.stderr)
    status = 0
sys.exit(status)
'''


def native_hook_directory(home):
    return Path(home) / ".config/agent-brain-client/native-hooks"


def native_hook_command(home, harness, settings=None):
    """Only stable paths and route arguments belong in native configuration."""
    from agent_brain_client.adapters.platforms.filesystem import command_line

    root = native_hook_directory(home)
    interpreter, bootstrap = str(root / "python.txt"), str(root / "launcher.py")
    args = [harness, *([str(settings)] if settings is not None else [])]
    if os.name == "nt":
        from agent_brain_client.adapters.platforms.windows.filesystem import powershell_command, powershell_quote
        return powershell_command(
            "& ([IO.File]::ReadAllText(" + powershell_quote(interpreter) + ").TrimEnd([char]10)) "
            + " ".join(map(powershell_quote, ["-X", "utf8", "-I", "-B", bootstrap, *args]))
        )
    return command_line([
        "/bin/sh", "-c",
        'IFS= read -r python < "$1" || exit; shift; exec "$python" -X utf8 -I -B "$@"',
        "agent-brain-hook", interpreter, bootstrap, *args,
    ])


def prepare_native_hook(home):
    """Reuse the original approved policy and immutable verifier for this enrollment."""
    import sys
    from agent_brain_client.adapters.platforms.filesystem import locked, private_directory, private_mode
    from agent_brain_client.adapters.storage import metadata
    from agent_brain_client.adapters.storage.host_files import atomic
    from agent_brain_client.adapters.storage.registration import registration
    from agent_brain_client.adapters.storage.private_files import raw as package_bytes

    home = metadata.canonical(home, existing=True)
    saved, _ = registration(home)
    policy = (saved["release_policy"], saved["release_policy_sha256"])
    root = native_hook_directory(home)
    private_directory(root)
    private_mode(root, directory=True, owner=True)
    receipt = root / "binding.json"
    with locked(root / "install.lock"):
        if receipt.exists():
            previous = metadata.private_json(receipt)
            if (set(previous) != {"policy", "files"} or previous["policy"] != list(policy)
                    or set(previous["files"]) != {"python.txt", "launcher.py"}):
                raise ValueError("Fixed Hook enrollment changed")
            for name, expected in previous["files"].items():
                path = metadata.canonical(root / name, existing=True)
                private_mode(path, owner=True)
                if metadata.digest(path.read_bytes()) != expected:
                    raise ValueError("Fixed Hook launcher changed")
            return
        channel = ClientReleases(*policy, inspection=True)
        if channel.policy["runtime"]["device_home"] != str(home):
            raise ValueError("Hook release identity changed")
        active = channel.verified_active(serving=True)
        if active is None:
            raise ValueError("Verified serving Hook release required")
        package = Path(active["package"])
        manifest = json.loads(package_bytes(package / "verified.json"))["manifest"]
        if metadata.digest(json.dumps(manifest, sort_keys=True).encode()) != active["manifest_sha256"]:
            raise ValueError("Fixed Hook anchor manifest changed")
        pins = {name: pin for name, pin in manifest["files"].items() if name.startswith("src/")}
        entry = package / "src/agent_brain_client/runtime/host_binding.py"
        if b"def run_native_hook(" not in entry.read_bytes():
            raise ValueError("Active release lacks the fixed Hook entry")
        runtime = channel.policy["runtime"]
        if "dependency_policy" in runtime:
            deps = metadata.private_json(runtime["dependency_policy"], runtime["dependency_policy_sha256"])
            python = deps["python"] if deps["schema"] == 2 else Path(deps["root"]) / "python/bin/python3"
        else:
            python = sys.executable
        executable = str(Path(python).resolve())
        if "\n" in executable or "\r" in executable:
            raise ValueError("Canonical Hook interpreter required")
        payload = {
            "python.txt": (executable + "\n").encode(),
            "launcher.py": (
                f"HOME = {str(home)!r}\nPOLICY = {policy!r}\nANCHOR = {str(package)!r}\nPINS = {pins!r}\n"
                + _BOOTSTRAP
            ).encode(),
        }
        for name, raw in payload.items():
            path = metadata.canonical(root / name)
            if path.exists():
                private_mode(path, owner=True)
                if path.read_bytes() != raw:
                    raise ValueError("Existing fixed Hook launcher preserved")
            else:
                atomic(path, raw, 0o600)
        metadata.write(receipt, {
            "policy": list(policy),
            "files": {name: metadata.digest(raw) for name, raw in payload.items()},
        }, exclusive=True)
