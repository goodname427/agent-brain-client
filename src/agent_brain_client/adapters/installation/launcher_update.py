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


# Both native commands launch OS tools first. The private installed binding
# supplies digests as data. Python executes the exact launcher bytes it verifies.
_LOAD_HOOK = '''
import hashlib, sys
from pathlib import Path
p = Path(sys.argv.pop(1))
pin = sys.argv.pop(1)
try:
    if p.resolve(strict=True) != p or p.stat().st_size > 65536:
        raise ValueError()
    raw = p.read_bytes()
    if hashlib.sha256(raw).hexdigest() != pin:
        raise ValueError()
except (OSError, ValueError):
    print('{}', flush=True)
    print('agent-brain Hook launcher integrity failed', file=sys.stderr)
    sys.exit(0)
exec(compile(raw, str(p), 'exec'))
'''
_MAC_HOOK_GUARD = '''
import hashlib, json, os, pathlib, stat, subprocess, sys

def private(path, limit=65536):
    st = path.lstat()
    if (path.resolve(strict=True) != path or not stat.S_ISREG(st.st_mode)
            or st.st_nlink != 1 or st.st_uid != os.getuid() or st.st_mode & 0o077
            or st.st_size > limit):
        raise ValueError('Private Hook binding required')
    return path.read_bytes()

def pinned(path, expected, limit=65536):
    raw = private(pathlib.Path(path), limit)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Hook binding changed')
    return json.loads(raw)

try:
    root = pathlib.Path(sys.argv[1])
    row = json.loads(private(root / 'binding.json'))
    saved = json.loads(private(root.parent / 'codex-mcp.json'))
    anchor = [saved['release_policy'], saved['release_policy_sha256']]
    if row['schema'] != 2 or row['policy'] != anchor:
        raise ValueError('Hook enrollment changed')
    runtime = pinned(*anchor)['runtime']
    if 'dependency_policy' in runtime:
        deps = pinned(runtime['dependency_policy'], runtime['dependency_policy_sha256'], 2097152)
        if deps['schema'] == 2:
            python_pin = deps['python'], deps['python_sha256']
        else:
            base = pathlib.Path(deps['root'])
            executable = (base / 'python/bin/python3').resolve(strict=True)
            python_pin = str(executable), deps['files'][executable.relative_to(base).as_posix()]['sha256']
        if (row['python'], row['python_sha256']) != python_pin:
            raise ValueError('Hook interpreter binding changed')
    python = pathlib.Path(row['python'])
    if (not python.is_absolute() or python.resolve(strict=True) != python
            or hashlib.sha256(python.read_bytes()).hexdigest() != row['python_sha256']):
        raise ValueError('Prepared Hook interpreter changed')
    rc = subprocess.run([
        str(python), '-X', 'utf8', '-I', '-S', '-B', '-c', sys.argv[2],
        str(root / 'launcher.py'), row['launcher_sha256'], os.getcwd(), *sys.argv[3:],
    ], cwd='/usr/bin', check=False).returncode
except Exception:
    print('{}', flush=True)
    print('agent-brain Hook bootstrap verification failed', file=sys.stderr)
    rc = 0
sys.exit(rc)
'''


# The first fixed launcher can be retained during a verified rollback to the
# immediate predecessor. Retire this adapter when dropping 0.8.26 rollback.
_MAC_0826 = (
    "be7957ec61b3b776ada0903f626a3c72d3d0972d",
    "be1b7e52dc108a1ea7bc704b93b841f2c2415006d8ccea28af1a0f646fd0214d",
)
_WINDOWS_0826 = (
    "22b7522e93bac0812907192433016ea45c541d9a",
    "b8c86e229933e17c1ace09a4d5963a08c8829064c1511aa6535892f162dfe0ff",
)
_PUBLISHED_0826 = {
    "darwin-arm64": _MAC_0826,
    "darwin-x86_64": _MAC_0826,
    "win32-amd64": _WINDOWS_0826,
    "win32-arm64": _WINDOWS_0826,
}
_LEGACY_HOOK = '''
import os
import sys

sys.path.insert(0, sys.argv.pop(1))
os.chdir(sys.argv.pop(1))
home, harness, *project = sys.argv[1:]
if project:
    from agent_brain_client.interfaces.cli import project as cli
    settings, pin = project
    sys.argv[1:] = ['hook', '--harness', harness, '--settings', settings, '--settings-sha256', pin]
    status = cli.main()
elif harness == 'codex':
    from agent_brain_client.interfaces.cli import collection as cli

    # Serving verification covered the actual executable's code. Keep the
    # legacy settings/approval/identity checks without requiring forward pins.
    original_load = cli.core.load
    cli.core.load = lambda home, *, verify_code=True: original_load(home, verify_code=False)
    status = cli.main(['global-hook', '--harness', harness, '--device-home', home])
else:
    from agent_brain_client.interfaces.cli import codebuddy as cli
    from agent_brain_client.runtime.collection import native_services

    cli.host_binding.load_core = lambda home: native_services(verify_code=False)
    sys.argv[1:] = ['hook', '--device-home', home]
    status = cli.main()
sys.exit(status)
'''


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
    cwd, harness, *project = sys.argv[1:]
    if harness not in ("codex", "codebuddy") or len(project) > 1:
        raise ValueError("Explicit native Hook route required")
    if entry.is_file() and b"def run_native_hook(" in entry.read_bytes():
        code = (
            "import os,sys; sys.path.insert(0,sys.argv.pop(1)); os.chdir(sys.argv.pop(1)); "
            "from agent_brain_client.runtime.host_binding import run_native_hook; "
            "sys.exit(run_native_hook(*sys.argv[1:]))"
        )
    elif (active["version"] == "0.8.26"
          and (active["commit"], active["manifest_sha256"]) == PUBLISHED_0826.get(channel.policy["platform"])):
        if project:
            from agent_brain_client.adapters.storage.project_settings import installed_hook_pin

            project.append(installed_hook_pin(HOME, harness, project[0]))
        print("agent-brain Hook serving verified 0.8.26 rollback", file=sys.stderr)
        code = LEGACY_HOOK
    else:
        raise ValueError("Verified Hook entry required")
    status = subprocess.run(
        [sys.executable, "-X", "utf8", "-I", "-B", "-c", code, str(package / "src"),
         cwd, HOME, harness, *project], check=False,
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
    args = [harness, *([str(settings)] if settings is not None else [])]
    if os.name == "nt":
        from agent_brain_client.adapters.platforms.windows.filesystem import powershell_command, powershell_quote

        binding = powershell_quote(str(root / "binding.json"))
        registration = powershell_quote(str(root.parent / "codex-mcp.json"))
        launcher = powershell_quote(str(root / "launcher.py"))
        invocation = (
            "$ErrorActionPreference='Stop'; try { "
            "$cwd=$PWD.ProviderPath; "
            "$env:PSModulePath=[IO.Path]::Combine($PSHOME,'Modules'); "
            "Set-Location -LiteralPath $PSHOME; [Environment]::CurrentDirectory=$PSHOME; "
            "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
            "$allowed=@($sid,'S-1-5-18','S-1-5-32-544'); "
            "function Read-ABBinding($path,$pin,$limit=65536) { "
            "$item=[IO.FileInfo]::new($path); "
            "if ($item.FullName -cne $path) { throw 'Canonical Hook binding required' }; "
            "for($node=$item;$null -ne $node;$node=$(if($node -is [IO.FileInfo]){$node.Directory}else{$node.Parent})) "
            "{ if($node.Attributes -band [IO.FileAttributes]::ReparsePoint){ throw 'Linked Hook binding' } }; "
            "$acl=[IO.File]::GetAccessControl($item.FullName); "
            "if ($item.Length -gt $limit -or $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid "
            "-or ($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) "
            "| Where-Object { $_.AccessControlType -eq 'Allow' -and $_.IdentityReference.Value -notin $allowed })) "
            "{ throw 'Private Hook binding required' }; "
            "$raw=[IO.File]::ReadAllBytes($item.FullName); "
            "if($pin -and (Get-ABHash $raw) -ine $pin){ throw 'Hook binding changed' }; "
            "return ([Text.Encoding]::UTF8.GetString($raw) | ConvertFrom-Json) }; "
            "function Get-ABHash($raw) { $sha=[Security.Cryptography.SHA256]::Create(); "
            "try { return [BitConverter]::ToString($sha.ComputeHash([byte[]]$raw)).Replace('-','') } "
            "finally { $sha.Dispose() } }; "
            "$row=Read-ABBinding " + binding + " $null; "
            "$saved=Read-ABBinding " + registration + " $null; "
            "if ($row.schema -ne 2 -or $row.policy[0] -cne $saved.release_policy "
            "-or $row.policy[1] -cne $saved.release_policy_sha256) { throw 'Hook enrollment changed' }; "
            "$runtime=(Read-ABBinding $row.policy[0] $row.policy[1]).runtime; "
            "if($runtime.dependency_policy){ "
            "$deps=Read-ABBinding $runtime.dependency_policy $runtime.dependency_policy_sha256 2097152; "
            "if($deps.schema -eq 2 -and ($deps.python -cne $row.python "
            "-or $deps.python_sha256 -cne $row.python_sha256)){ throw 'Hook interpreter binding changed' } }; "
            "if ($row.python -notmatch '^[A-Za-z]:' -or [IO.Path]::GetFullPath($row.python) -cne $row.python "
            "-or ((Get-Item -LiteralPath $row.python).Attributes -band [IO.FileAttributes]::ReparsePoint) "
            "-or (Get-FileHash -Algorithm SHA256 -LiteralPath $row.python).Hash -ine $row.python_sha256) "
            "{ throw 'Prepared Hook interpreter changed' }; "
            "& $row.python -X utf8 -I -S -B -c " + powershell_quote(_LOAD_HOOK)
            + " " + launcher + " $row.launcher_sha256 $cwd " + " ".join(map(powershell_quote, args))
            + "; exit $LASTEXITCODE } catch { "
            "[Console]::Out.WriteLine('{}'); [Console]::Error.WriteLine('agent-brain Hook bootstrap verification failed'); exit 0 }"
        )
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        return powershell_command(invocation, executable=str(powershell))
    return command_line([
        "/usr/bin/env", "-u", "DEVELOPER_DIR", "-u", "TOOLCHAINS",
        "/usr/bin/python3", "-I", "-S", "-B", "-c", _MAC_HOOK_GUARD,
        str(root), _LOAD_HOOK, *args,
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
            if (set(previous) != {"schema", "policy", "python", "python_sha256", "launcher_sha256"}
                    or previous["schema"] != 2 or previous["policy"] != list(policy)):
                raise ValueError("Fixed Hook enrollment changed")
            path = metadata.canonical(root / "launcher.py", existing=True)
            private_mode(path, owner=True)
            if metadata.digest(path.read_bytes()) != previous["launcher_sha256"]:
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
        launcher = (
            f"HOME = {str(home)!r}\nPOLICY = {policy!r}\nANCHOR = {str(package)!r}\nPINS = {pins!r}\n"
            f"PUBLISHED_0826 = {_PUBLISHED_0826!r}\nLEGACY_HOOK = {_LEGACY_HOOK!r}\n"
            + _BOOTSTRAP
        ).encode()
        path = metadata.canonical(root / "launcher.py")
        if path.exists():
            private_mode(path, owner=True)
            if path.read_bytes() != launcher:
                raise ValueError("Existing fixed Hook launcher preserved")
        else:
            atomic(path, launcher, 0o600)
        metadata.write(receipt, {
            "schema": 2,
            "policy": list(policy),
            "python": executable,
            "python_sha256": metadata.digest(Path(executable).read_bytes()),
            "launcher_sha256": metadata.digest(launcher),
        })
