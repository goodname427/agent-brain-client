"""Update the owned fixed launcher using an exact recoverable transaction."""

import json
import os
from pathlib import Path
import subprocess
from agent_brain_client.adapters.packages.client import ClientReleases
from agent_brain_client.adapters.storage.private_files import private_json, safe
import agent_brain_client.adapters.storage.private_files as private_files
from agent_brain_client.adapters.platforms.credentials import canonical


def completed_reinstall_registration(home, current, meta):
    """Recognize only the current install or its exact transaction predecessor."""
    from agent_brain_client.adapters.storage.install_state import sha

    if current["plan_sha256"] == meta["plan_sha256"]:
        return meta
    import base64
    import agent_brain_client.adapters.harnesses.codex.installation as ci

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
    import agent_brain_client.adapters.harnesses.codex.installation as ci
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


def refresh_launcher(
    home, *, channel_factory=None, channel=None, before=None, meta=None
):
    import agent_brain_client.adapters.harnesses.codex.installation as ci
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
    import agent_brain_client.adapters.harnesses.codex.installation as ci

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
        plan = ci.prepare(
            home,
            candidate,
            meta["release_policy"],
            meta["release_policy_sha256"],
            replace_existing=True,
        )
        _, registered, _, _ = ci.registered_context(home, meta)
        if str(candidate) == meta["package"] and plan["mcp"] == registered:
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
