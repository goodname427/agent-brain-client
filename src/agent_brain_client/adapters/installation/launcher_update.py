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
    launcher_package_files,
    managed_worker_abi,
    single_entry_pin,
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
        entry_pin = single_entry_pin(installed, launcher_package_files(installed))
        # Worker compatibility is negotiated on the existing MCP session.
        return entry_pin is not None and entry_pin == launcher_package_files(candidate).get(MCP_LAUNCHER_ENTRY)
    except (OSError, ValueError, KeyError, TypeError, UpdateBlocked):
        return False


def can_activate_at_startup(home, active, candidate, manifest):
    """Keep native registration changes in the public maintenance transaction."""
    import agent_brain_client.adapters.installation.installation as ci

    if active is None:
        return False
    previous = Path(active["package"])
    abi = managed_worker_abi(previous, launcher_package_files(previous))
    if abi is None or abi != managed_worker_abi(candidate, manifest["files"]):
        return False
    meta = private_json(safe(Path(home) / ".config/agent-brain-client/codex-mcp.json"))
    policy, registered, _, _ = ci.registered_context(home, meta)
    harness = ci.command_harness(registered)
    args = (meta["release_policy"], meta["release_policy_sha256"])
    _, _, _, _, previous_contract = ci._package_context(
        home, previous, *args, inspection=True, harness=harness,
    )
    _, desired, _, _, candidate_contract = ci._package_context(
        home, candidate, *args, inspection=True, harness=harness,
    )
    return previous_contract == candidate_contract and can_retain_launcher(
        policy, meta["package"], registered, candidate, desired, active,
    )


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


# This code is part of the host-approved command. It verifies the registered
# MCP package before importing Python from it, without a mutable launcher file.
_LOAD_HOOK = '''
import hashlib, json, pathlib, sys

def read(path, limit):
    if (not path.is_absolute() or path.resolve(strict=True) != path
            or not path.is_file() or path.stat().st_size > limit):
        raise ValueError('Noncanonical Hook runtime path')
    return path.read_bytes()

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

try:
    home, cwd, registry_pin, harness, *project = sys.argv[1:]
    state = pathlib.Path(home) / '.config/agent-brain-client'
    original = read(state / 'codex-mcp.json', 1048576)
    if digest(original) != registry_pin:
        raise ValueError('Hook enrollment changed')
    saved = json.loads(original)
    package = pathlib.Path(saved['package'])
    receipt = package / 'verified.json'
    if not receipt.exists():
        receipt = package / 'export-manifest.json'
    manifest = json.loads(read(receipt, 262144))
    files = manifest.get('manifest', manifest)['files']
    if digest(json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()) != saved['package_sha256']:
        raise ValueError('Registered MCP package changed')
    src = package / 'src'
    paths = list(src.rglob('*'))
    pins = {n: h for n, h in files.items() if n.startswith('src/')}
    if (any(p.is_symlink() for p in paths)
            or {p.relative_to(package).as_posix() for p in paths if not p.is_dir()} != pins.keys()):
        raise ValueError('Registered MCP package membership changed')
    for name, expected in pins.items():
        if digest(read(package / name, 2097152)) != expected:
            raise ValueError('Registered MCP package code changed')
    if read(state / 'codex-mcp.json', 1048576) != original:
        raise ValueError('Hook enrollment changed')
    sys.path.insert(0, str(src))
    from agent_brain_client.adapters.installation.launcher_update import native_hook_startup

    status = native_hook_startup(home, cwd, harness, project, saved)
except Exception as error:
    print('{}', flush=True)
    print('agent-brain Hook launch unavailable: ' + type(error).__name__, file=sys.stderr)
    status = 0
sys.exit(status)
'''

_MAC_HOOK_GUARD = '''
import hashlib, json, os, pathlib, stat, subprocess, sys

def read(path, limit):
    p = pathlib.Path(path)
    st = p.lstat()
    if (p.resolve(strict=True) != p or not stat.S_ISREG(st.st_mode)
            or st.st_nlink != 1 or st.st_uid != os.getuid() or st.st_mode & 0o077
            or st.st_size > limit):
        raise ValueError('Private Hook binding required')
    return p.read_bytes()

def pinned(path, expected, limit):
    raw = read(path, limit)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Hook binding changed')
    return json.loads(raw)

try:
    home, code, *args = sys.argv[1:]
    state = pathlib.Path(home) / '.config/agent-brain-client'
    registered = read(state / 'codex-mcp.json', 1048576)
    saved = json.loads(registered)
    runtime = pinned(saved['release_policy'], saved['release_policy_sha256'], 65536)['runtime']
    deps = pinned(runtime['dependency_policy'], runtime['dependency_policy_sha256'], 2097152)
    if deps['schema'] == 2:
        python = pathlib.Path(deps['python'])
        pin = deps['python_sha256']
    elif deps['schema'] == 1:
        base = pathlib.Path(deps['root'])
        python = (base / 'python/bin/python3').resolve(strict=True)
        pin = deps['files'][python.relative_to(base).as_posix()]['sha256']
    else:
        raise ValueError('Prepared Hook interpreter required')
    if not python.is_absolute() or python.resolve(strict=True) != python or hashlib.sha256(python.read_bytes()).hexdigest() != pin:
        raise ValueError('Prepared Hook interpreter changed')
    status = subprocess.run([str(python), '-X', 'utf8', '-I', '-S', '-B', '-c', code,
                             home, os.getcwd(), hashlib.sha256(registered).hexdigest(), *args],
                            cwd='/usr/bin', check=False).returncode
except Exception:
    print('{}', flush=True)
    print('agent-brain Hook bootstrap verification failed', file=sys.stderr)
    status = 0
sys.exit(status)
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


def native_hook_startup(home, cwd, harness, project, saved):
    """Select the serving snapshot after the approved MCP verifier is loaded."""
    import sys
    from time import monotonic
    from agent_brain_client.adapters.storage.registration import registration
    from agent_brain_client.application.control import RECALL_TIMING_ENV

    started = monotonic()
    timing = os.environ.get(RECALL_TIMING_ENV) == "1"
    current, _ = registration(home)
    if current != saved or harness not in ("codex", "codebuddy") or len(project) > 1:
        raise ValueError("Hook enrollment or route changed")
    channel = ClientReleases(saved["release_policy"], saved["release_policy_sha256"])
    if channel.policy["runtime"]["device_home"] != home:
        raise ValueError("Hook release identity changed")
    active = channel.verified_active(serving=True)
    if active is None:
        raise ValueError("Verified serving Hook release required")
    package = Path(active["package"])
    entry = package / "src/agent_brain_client/runtime/host_binding.py"
    if entry.is_file() and b"def run_native_hook(" in entry.read_bytes():
        code = (
            "import os,sys; sys.path.insert(0,sys.argv.pop(1)); os.chdir(sys.argv.pop(1)); "
            "from agent_brain_client.runtime.host_binding import run_native_hook; "
            "sys.exit(run_native_hook(*sys.argv[1:]))"
        )
    elif (active["version"] == "0.8.26"
          and (active["commit"], active["manifest_sha256"]) == _PUBLISHED_0826.get(channel.policy["platform"])):
        if project:
            from agent_brain_client.adapters.storage.project_settings import installed_hook_pin

            project.append(installed_hook_pin(home, harness, project[0]))
        print("agent-brain Hook serving verified 0.8.26 rollback", file=sys.stderr)
        code = _LEGACY_HOOK
    else:
        raise ValueError("Verified Hook entry required")
    verified = monotonic()
    if timing:
        print(json.dumps({"operation": "native-hook-startup", "stage": "serving-package-ready",
                          "elapsed_ms": round((verified - started) * 1000, 3)}),
              file=sys.stderr, flush=True)
    try:
        return subprocess.run(
            [sys.executable, "-X", "utf8", "-I", "-S", "-B", "-c", code,
             str(package / "src"), cwd, home, harness, *project], check=False,
        ).returncode
    finally:
        if timing:
            print(json.dumps({"operation": "native-hook-startup", "stage": "child-wait-ended",
                              "elapsed_ms": round((monotonic() - verified) * 1000, 3)}),
                  file=sys.stderr, flush=True)


def native_hook_command(home, harness, settings=None):
    """Render a stable native command over the installed MCP authority."""
    from agent_brain_client.adapters.platforms.filesystem import command_line

    args = [harness, *([str(settings)] if settings is not None else [])]
    if os.name == "nt":
        from agent_brain_client.adapters.platforms.windows.filesystem import powershell_command, powershell_quote

        registration = powershell_quote(str(Path(home) / ".config/agent-brain-client/codex-mcp.json"))
        # This Python literal contains no double quotes, which Windows PowerShell's legacy argument forwarding may otherwise drop.
        invocation = (
            "$ProgressPreference='SilentlyContinue';$ErrorActionPreference='Stop';try{$cwd=$PWD.ProviderPath;"
            "$env:PSModulePath=[IO.Path]::Combine($PSHOME,'Modules');"
            "Set-Location -LiteralPath $PSHOME;[Environment]::CurrentDirectory=$PSHOME;"
            "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;"
            "function ReadAB($p,$hash,$limit){$f=[IO.FileInfo]::new($p);"
            "if($f.FullName -cne $p -or $f.Length -gt $limit){throw 'Invalid binding'};"
            "for($i=$f;$null -ne $i;$i=$(if($i -is [IO.FileInfo]){$i.Directory}else{$i.Parent}))"
            "{if($i.Attributes -band 1024){throw 'Linked binding'}};"
            "$acl=[IO.File]::GetAccessControl($p);"
            "if($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid -or "
            "($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])|Where-Object"
            " {$_.AccessControlType -eq 'Allow' -and $_.IdentityReference.Value -notin @($sid,'S-1-5-18','S-1-5-32-544')}))"
            "{throw 'Private binding required'};"
            "$raw=[IO.File]::ReadAllBytes($p);$sha=[Security.Cryptography.SHA256]::Create();"
            "$script:abpin=[BitConverter]::ToString($sha.ComputeHash($raw)).Replace('-','').ToLowerInvariant();"
            "if($hash -and $script:abpin -ine $hash){throw 'Binding changed'};"
            "[Text.Encoding]::UTF8.GetString($raw)|ConvertFrom-Json};"
            "$r=ReadAB " + registration + " $null 1048576;$registryPin=$script:abpin;"
            "$p=(ReadAB $r.release_policy $r.release_policy_sha256 65536).runtime;"
            "$d=ReadAB $p.dependency_policy $p.dependency_policy_sha256 2097152;$exe=$d.python;"
            "if($d.schema -ne 2 -or $exe -notmatch '^[A-Za-z]:' -or [IO.Path]::GetFullPath($exe) -cne $exe"
            " -or ((Get-Item -LiteralPath $exe).Attributes -band 1024)"
            " -or (Get-FileHash -LiteralPath $exe).Hash -ine $d.python_sha256){throw 'Interpreter changed'};"
            "& $exe -X utf8 -I -S -B -c " + powershell_quote(_LOAD_HOOK)
            + " " + powershell_quote(str(home)) + " $cwd $registryPin " + " ".join(map(powershell_quote, args))
            + ";exit $LASTEXITCODE}catch{[Console]::Out.WriteLine('{}');"
            "[Console]::Error.WriteLine('agent-brain Hook bootstrap verification failed');exit 0}"
        )
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        command = powershell_command(invocation, executable=str(powershell), compact=True)
        if len(command.encode("utf-16le")) // 2 > 8120:  # room for cmd.exe /C and outer quotes
            raise ValueError("Native Hook registration exceeds the Windows cmd.exe length limit")
        return command
    return command_line([
        "/usr/bin/env", "-u", "DEVELOPER_DIR", "-u", "TOOLCHAINS",
        "/usr/bin/python3", "-I", "-S", "-B", "-c", _MAC_HOOK_GUARD,
        str(home), _LOAD_HOOK, *args,
    ])


def prepare_native_hook(home):
    """Validate the existing managed interpreter and installed MCP trust root."""
    from agent_brain_client.adapters.installation.installation import package_check
    from agent_brain_client.adapters.storage.registration import registration

    saved, policy = registration(home)
    pending = Path(home) / ".config/agent-brain-client/launcher-handover.json"
    if pending.exists() or pending.is_symlink():
        raise ValueError("Complete the existing MCP launcher handover before installing Hooks")
    channel = ClientReleases(saved["release_policy"], saved["release_policy_sha256"], inspection=True)
    if channel.policy["runtime"]["device_home"] != str(home) or "dependency_policy" not in policy["runtime"]:
        raise ValueError("The existing verified managed interpreter is required")
    package = safe(saved["package"])
    entry = package / "src/agent_brain_client/adapters/installation/launcher_update.py"
    if (package_check(package, policy) != saved["package_sha256"]
            or b"def native_hook_startup(" not in entry.read_bytes()):
        raise ValueError("Run the normal Client update to refresh the installed MCP launcher before migrating Hooks")
