import agent_brain_client.adapters.storage.private_files as adapters_storage_private_files

"""runtime / updates: concrete installation responsibility."""

import sys
from pathlib import Path
import json
import os
import platform as platform  # Kept as the historical installer inspection seam.
import subprocess
from agent_brain_client.adapters.packages.client import (
    ClientReleases,
    HTTPS_CONTENT_CAPABILITIES,
    dependency_environment,
    CHECK_UPDATE_COMMAND,
    UPDATE_COMMAND,
    DEPENDENCY_RUNTIME,
)
from agent_brain_client.adapters.storage.private_files import private_json, safe
from agent_brain_client.adapters.platforms.credentials import canonical

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT
OFFICIAL_ORIGIN = "https://github.com/goodname427/agent-brain-client.git"
OFFICIAL_REF = "refs/heads/releases/personal-https-stable"


def reexec_update_runtime(home, action, *, runner=subprocess.run):
    """Use the originally pinned Python/Git without changing their binding."""
    from agent_brain_client.adapters.harnesses.codex.installation import (
        inspect,
        registered_context,
    )

    if not inspect(home).get("registration_verified"):
        if action != "update":
            return None
        import agent_brain_client.adapters.harnesses.codex.installation as content_install

        # A torn inline Hook can make live TOML unreadable. Authenticate its
        # exact saved MCP preimage to select the original interpreter before
        # manage_update performs any recovery writes.
        from agent_brain_client.adapters.storage.collection import settings_path
        import agent_brain_client.adapters.harnesses.codex.collection_installation as hooks

        hook_receipt = settings_path(home).parent / "hook-install.json"
        if hook_receipt.exists():
            record = hooks.read_receipt(hook_receipt)
            if record.get("phase") == "applying":
                import base64

                _, hook_policy = hooks.old.registration(home)
                native = Path(hook_policy["runtime"]["codex_home"])
                if set(record.get("host", {})) != {str(native / "config.toml")}:
                    raise ValueError("Hook recovery binding changed")
                before = base64.b64decode(
                    record["host"][str(native / "config.toml")]["before"], validate=True
                )
                meta = private_json(
                    safe(Path(home) / ".config/agent-brain-client/codex-mcp.json")
                )
                policy, mcp, _, native = registered_context(
                    home, meta, config_image=before
                )
                return pinned_update_runtime(home, action, policy, mcp, native, runner)
        handover = safe(
            Path(home) / ".config/agent-brain-client/bootstrap-handover-plan.json"
        )
        launcher = safe(
            Path(home) / ".config/agent-brain-client/launcher-handover.json"
        )
        if launcher.exists():
            handover = launcher
        if not handover.exists():
            return None
        try:
            saved = adapters_storage_private_files.private_json(handover)
            plan = saved["plan"] if handover == launcher else saved
            plan_digest = content_install.sha(content_install.contract.encode(plan))
            content_install.validate_install_scope(plan)
            state = Path(home) / ".config/agent-brain-client"
            receipt = adapters_storage_private_files.private_json(
                safe(state / "content-install-receipt.json")
            )
            if (
                receipt.get("phase") == "applying"
                and receipt.get("plan_sha256") != plan_digest
            ):
                return None
            meta = private_json(safe(state / "codex-mcp.json"))
            if (
                meta.get("content_candidate") is not True
                or plan.get("device_home") != str(Path(home).resolve())
                or plan.get("release_policy") != meta.get("release_policy")
                or plan.get("release_policy_sha256")
                != meta.get("release_policy_sha256")
            ):
                return None
            from agent_brain_client.adapters.packages.client import policy_file

            policy = policy_file(
                meta["release_policy"],
                meta["release_policy_sha256"],
                runtime_check=False,
            )
            if not DEPENDENCY_RUNTIME <= set(policy["runtime"]):
                return None
            environment = dependency_environment(policy, runtime_check=False)
            dependencies = adapters_storage_private_files.private_json(
                safe(policy["runtime"]["dependency_policy"])
            )
            python = str(Path(dependencies["python"]).resolve())
            if plan.get("mcp", {}).get("command") != python:
                return None
            executable = safe(Path(python))
            if not executable.is_file() or not os.access(executable, os.X_OK):
                return None
            if executable.resolve() == Path(sys.executable).resolve():
                return None
            environment = dict(
                environment,
                HOME=str(home),
                CODEX_HOME=plan["codex_home"],
                LANG="en_US.UTF-8",
            )
            return runner(
                [
                    python,
                    "-I",
                    "-B",
                    str(ROOT / "src/agent_brain_client/interfaces/cli/install.py"),
                    "--action",
                    action,
                ],
                env=environment,
                stdin=None
                if action == "update" and sys.stdin.isatty()
                else subprocess.DEVNULL,
            ).returncode
        except Exception:
            return None
    meta = private_json(safe(Path(home) / ".config/agent-brain-client/codex-mcp.json"))
    policy, mcp, _, native = registered_context(home, meta)
    return pinned_update_runtime(home, action, policy, mcp, native, runner)


def pinned_update_runtime(home, action, policy, mcp, native, runner):
    """Re-execute only the registered interpreter with its original bindings."""
    if not DEPENDENCY_RUNTIME <= set(policy["runtime"]):
        return None
    environment = dependency_environment(policy, runtime_check=False)
    if Path(mcp["command"]).resolve() == Path(sys.executable).resolve():
        return None
    environment = dict(
        environment, HOME=str(home), CODEX_HOME=str(native), LANG="en_US.UTF-8"
    )
    return runner(
        [
            mcp["command"],
            "-I",
            "-B",
            str(ROOT / "src/agent_brain_client/interfaces/cli/install.py"),
            "--action",
            action,
        ],
        env=environment,
        stdin=None if action == "update" and sys.stdin.isatty() else subprocess.DEVNULL,
    ).returncode


def completed_reinstall_registration(home, current, meta):
    """Recognize only the current install or its exact transaction predecessor."""
    from agent_brain_client.adapters.storage.install_state import sha

    if current["plan_sha256"] == meta["plan_sha256"]:
        return meta
    import base64
    import agent_brain_client.adapters.harnesses.codex.installation as ci

    registration = safe(Path(home) / ".config/agent-brain-client/codex-mcp.json")
    receipt = private_json(registration.parent / "content-install-receipt.json")
    registered = adapters_storage_private_files.raw(registration)
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


def windows_launcher_handover(
    home, *, channel_factory=None, channel=None, before=None, meta=None
):
    import agent_brain_client.adapters.harnesses.codex.installation as ci
    from agent_brain_client.adapters.storage.host_files import HostConfigChanged

    for attempt in range(3):
        try:
            return launcher_handover_once(
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


def launcher_handover_once(
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
        saved = adapters_storage_private_files.private_json(marker)
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
        adapters_storage_private_files.save(marker, saved)
    plan = saved["plan"]
    # An editor may have added tools since the updater saved this plan. Refresh
    # only config preimages; prepare authenticates our own service independently
    # of every unrelated TOML field. Interrupted writes use exact recovery first.
    refreshed = ci.rebase_update_plan(plan)
    if refreshed != plan:
        saved = dict(saved, plan=refreshed)
        adapters_storage_private_files.save(marker, saved)
        plan = refreshed
    target = safe(plan["package"])
    if target.parent != safe(Path(channel.engine.local) / "packages"):
        raise ValueError("Launcher package outside trusted cache")
    ci.package_check(target, channel.engine.policy)
    verified = json.loads(adapters_storage_private_files.raw(target / "verified.json"))
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
    receipt = adapters_storage_private_files.private_json(
        Path(home) / ".config/agent-brain-client/content-install-receipt.json"
    )
    metadata = adapters_storage_private_files.private_json(
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
    metadata = adapters_storage_private_files.private_json(
        Path(home) / ".config/agent-brain-client/codex-mcp.json"
    )
    if metadata["package"] != str(target) or metadata["plan_sha256"] != pin:
        raise ValueError("Launcher registration target changed")
    if adapters_storage_private_files.private_json(marker) != saved:
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


def manage_update(home, action, *, channel_factory=None):
    result = _manage_update(home, action, channel_factory=channel_factory)
    # The installer observes disk/config changes, never the active host session.
    # A changed package can already be loaded by a new worker by this point.
    result["reload_required"] = None
    result["reload_reason"] = "running-worker-unobserved"
    result["reload_check"] = {
        "active_package_changed": result.get("active_package_changed"),
        "mcp_registration_changed": result.get("bootstrap_registration_changed"),
        "hook_configuration_changed": result.get(
            "hook_configuration_changed",
            result.get("global_hook", {}).get("native_changed"),
        ),
        "host_tool_visibility": "unknown",
        "model_loaded": "unknown",
    }
    if result.get("status") in (
        "client-update-updated",
        "client-update-current",
        "client-update-available",
    ):
        result["next_action"] = (
            (
                "Run " + UPDATE_COMMAND + " to apply the trusted candidate. "
                if result.get("update_available")
                else "The verified local package is ready. "
            )
            + "Call an already available Agent Brain MCP tool and inspect its fresh client_runtime. "
            "Compare running_worker_version AND running_worker_commit with active_package_version AND active_package_commit. "
            "If both match, no MCP worker reload is needed. If they differ, normally reload only Agent Brain MCP after in-flight work completes. "
            "If the worker or commit is unobservable, report unknown; do not request a restart merely because update ran. "
            "offered_tools describes the worker; compare with the tools actually available in this Codex session. "
            "If a required offered tool is missing, refresh the host tool connection using normal host controls. "
            "Hook configuration/trust and mapped Rules/Skills have separate host loading states; review them only when changed. "
            "Do not infer model context loading from a package or worker version."
        )
    if action == "update" and result.get("status") in (
        "client-update-current",
        "client-update-updated",
    ):
        from types import SimpleNamespace
        import agent_brain_client.adapters.harnesses.codex.collection_installation as hooks
        from agent_brain_client.runtime.installation_completion import source_setup

        try:
            from agent_brain_client.adapters.storage.collection import settings_path

            if (
                not settings_path(home).exists()
                or result.get("global_hook", {}).get("status")
                == "global-hook-not-installed"
            ):
                result["global_hook"] = hooks.install(
                    SimpleNamespace(
                        device_home=str(home),
                        profile_id=None,
                        operator=None,
                        operator_sha256=None,
                    )
                )
                changed = result["global_hook"].get("native_changed", False)
                result["native_changed"] = (
                    result.get("native_changed", False) or changed
                )
                result["hook_configuration_changed"] = (
                    result.get("hook_configuration_changed", False) or changed
                )
                result["reload_check"]["hook_configuration_changed"] = result[
                    "hook_configuration_changed"
                ]
            result["source_setup"] = source_setup(home)
        except Exception:
            result["source_setup"] = {
                "status": "global-memory-connection-pending",
                "collection_enabled": False,
                "next_action": "Retry the same update entry; existing configuration is retained.",
            }
    return result


def config_conflict(result, field, reason="managed-config-conflict"):
    return dict(
        result,
        status="client-update-review-required",
        reason=reason,
        conflict_field=field,
        update_available=None,
        next_action="Resolve the conflicting Agent Brain setting at "
        + field
        + ", then rerun the same update entry. Existing user configuration and source approval are preserved.",
    )


def config_busy(result):
    return dict(
        result,
        status="client-update-review-required",
        reason="config-edit-in-progress",
        update_available=None,
        next_action="config.toml is still being edited. Let the editor finish, then rerun the same update entry; concurrent changes were preserved.",
    )


def _manage_update(home, action, *, channel_factory=None):
    """Reuse the registered updater; no pairing or worker restart.

    Discovery may cache verified candidates, but never changes the active pointer.
    The CLI cannot observe host worker versions. MCP responses report their own.
    """
    from agent_brain_client.adapters.storage.install_state import sha
    import agent_brain_client.adapters.harnesses.codex.installation as content_install
    import agent_brain_client.adapters.harnesses.codex.collection_installation as hooks
    from agent_brain_client.adapters.storage.host_files import HostConfigChanged
    from agent_brain_client.adapters.harnesses.codex.installation import (
        inspect,
        registered_context,
    )

    result = {
        "operation": action,
        "status": "client-update-review-required",
        "active_package_version": None,
        "active_package_commit": None,
        "running_worker_version": None,
        "running_worker_commit": None,
        "latest_channel_version": None,
        "latest_channel_commit": None,
        "update_available": None,
        "reload_required": None,
        "channel_checked": False,
        "active_package_changed": False,
        "native_changed": False,
        "device_credentials_accessed": False,
        "next_action": "Preserve the existing installation. Verify its registration, policy and bootstrap; do not replace trust metadata or re-pair.",
    }
    bootstrap_migrated = False
    hook_changed = False
    try:
        if action not in ("check-update", "update"):
            raise ValueError("Known maintenance action required")
        if hooks.recover_update(home, check_only=action == "check-update"):
            if action == "check-update":
                return dict(
                    result, reason="global-hook-recovery-required", native_changed=None
                )
            hook_changed = True
            result["native_changed"] = True
        launcher_path = safe(
            Path(home) / ".config/agent-brain-client/launcher-handover.json"
        )
        if launcher_path.exists():
            if action != "update":
                return dict(
                    result,
                    reason="launcher-handover-recovery-required",
                    native_changed=None,
                )
            bootstrap_migrated = windows_launcher_handover(
                home, channel_factory=channel_factory
            )
            result["native_changed"] = bootstrap_migrated
        handover_path = safe(
            Path(home) / ".config/agent-brain-client/bootstrap-handover-plan.json"
        )
        if handover_path.exists():
            if action != "update":
                return dict(
                    result,
                    reason="bootstrap-handover-recovery-required",
                    native_changed=None,
                )
            # The transaction journal may be applying, which makes inspect()
            # intentionally fail closed. Resume from the exact private plan
            # saved before the first transaction attempt.
            plan = adapters_storage_private_files.private_json(handover_path)
            plan_digest = content_install.sha(content_install.contract.encode(plan))
            metadata = private_json(
                safe(Path(home) / ".config/agent-brain-client/codex-mcp.json")
            )
            if (
                metadata.get("content_candidate") is not True
                or plan.get("device_home") != str(Path(home).resolve())
                or plan.get("release_policy") != metadata.get("release_policy")
                or plan.get("release_policy_sha256")
                != metadata.get("release_policy_sha256")
                or plan.get("replace_existing") is not True
            ):
                return dict(
                    result,
                    reason="bootstrap-handover-recovery-binding-required",
                    native_changed=None,
                )
            recovery_channel = (
                channel_factory(
                    metadata["release_policy"], metadata["release_policy_sha256"]
                )
                if channel_factory
                else ClientReleases(
                    metadata["release_policy"],
                    metadata["release_policy_sha256"],
                    inspection=True,
                )
            )
            active = recovery_channel.verified_active()
            package_root = safe(Path(recovery_channel.engine.local) / "packages")
            candidate = safe(plan["package"])
            if (
                active is None
                or not recovery_channel.engine.adapter.legacy_030_https_policy
                or candidate.parent != package_root
                or not candidate.is_dir()
            ):
                return dict(
                    result,
                    reason="bootstrap-handover-recovery-checkpoint-required",
                    native_changed=None,
                )
            content_install.package_check(candidate, recovery_channel.engine.policy)
            receipt_path = safe(
                Path(home) / ".config/agent-brain-client/content-install-receipt.json"
            )
            receipt = adapters_storage_private_files.private_json(receipt_path)
            if (
                receipt.get("phase") == "complete"
                and receipt.get("plan_sha256") == plan_digest
            ):
                verified = json.loads(
                    (candidate / "verified.json").read_text(encoding="utf-8")
                )
                candidate_manifest = verified["manifest"]
                candidate_is_active = (
                    active.get("version") == candidate_manifest.get("version")
                    and active.get("commit") == verified.get("commit")
                    and active.get("package") == str(candidate.resolve())
                )
                if active.get("version") != "0.3.0" and not candidate_is_active:
                    return dict(
                        result,
                        reason="bootstrap-handover-recovery-checkpoint-required",
                        native_changed=None,
                    )
                # apply()'s completed-plan fast path defines installed as
                # enabled. A paused registration is still a valid completed
                # handover and can safely continue to release activation. If
                # the host already started the new bootstrap, accept only this
                # exact verified candidate as the advanced active package.
                recovered_state = content_install.inspect(home)
                recovered = (
                    {
                        "status": "content-native-client-installed",
                        "installed": True,
                        "native_changed": False,
                    }
                    if recovered_state.get("registration_verified")
                    and recovered_state.get("installation_state")
                    in ("verified", "verified-disabled")
                    else {}
                )
            else:
                if active.get("version") != "0.3.0":
                    return dict(
                        result,
                        reason="bootstrap-handover-recovery-checkpoint-required",
                        native_changed=None,
                    )
                recovered = content_install.apply(
                    plan, plan_digest, check=content_install.probe_bootstrap
                )
            if recovered.get(
                "status"
            ) != "content-native-client-installed" or not recovered.get("installed"):
                return dict(
                    result,
                    reason="bootstrap-handover-recovery-incomplete",
                    native_changed=recovered.get("native_changed"),
                )
            bootstrap_migrated = True
            result["native_changed"] = True
            if not content_install.inspect(home).get("registration_verified"):
                return dict(
                    result,
                    reason="bootstrap-handover-recovery-verification-failed",
                    native_changed=True,
                )
            if content_install.sha(handover_path.read_bytes()) != plan_digest:
                return dict(
                    result,
                    reason="bootstrap-handover-plan-changed-after-recovery",
                    native_changed=None,
                )
            handover_path.unlink()
        state = inspect(home)
        if not state.get("registration_verified") or state.get(
            "installation_state"
        ) not in ("verified", "verified-disabled"):
            if state.get("reason") == "mcp-binding-changed":
                return config_conflict(
                    result, "mcp_servers." + content_install.NAME, state["reason"]
                )
            return dict(
                result, reason=state.get("reason", "verified-installation-required")
            )
        meta = private_json(
            safe(Path(home) / ".config/agent-brain-client/codex-mcp.json")
        )
        policy, registered_mcp, _, _ = registered_context(home, meta)
        launcher_manifest = Path(meta["package"]) / "verified.json"
        launcher_record = json.loads(
            adapters_storage_private_files.raw(
                launcher_manifest
                if launcher_manifest.exists()
                else Path(meta["package"]) / "export-manifest.json"
            )
        )
        result.update(
            registered_launcher_package=meta["package"],
            registered_launcher_version=launcher_record.get(
                "manifest", launcher_record
            ).get("version"),
            registered_tool_count=len(registered_mcp["enabled_tools"]),
        )
        if policy["capabilities"] != HTTPS_CONTENT_CAPABILITIES:
            return dict(result, reason="existing-https-installation-required")
        environment = dependency_environment(policy)
        previous = dict(os.environ)
        try:
            if environment is not None:
                os.environ.clear()
                os.environ.update(environment, HOME=str(home), LANG="en_US.UTF-8")
            # Explicit maintenance can wait through ordinary network jitter;
            # automatic MCP startup retains its short best-effort budget.
            channel = (
                channel_factory(meta["release_policy"], meta["release_policy_sha256"])
                if channel_factory
                else ClientReleases(
                    meta["release_policy"],
                    meta["release_policy_sha256"],
                    discovery_timeout=15,
                )
            )
            before = channel.verified_active()
            if before is None:
                return dict(result, reason="verified-active-package-required")
            result.update(
                active_package_version=before["version"],
                active_package_commit=before.get("commit"),
            )
            result["tool_discovery"] = content_install.tool_discovery(
                home, meta, before
            )
            if action == "check-update":
                discovered = channel.discover(
                    "codex", policy["platform"], before["version"]
                )
                rows = discovered.get("releases", [])
                if (
                    discovered.get("status") not in ("current", "compatible-update")
                    or len(rows) != 1
                ):
                    return dict(
                        result,
                        status="client-update-unavailable"
                        if discovered.get("status") == "release-unavailable"
                        else "client-update-review-required",
                        next_action="The trusted channel was not confirmed. Keep the current package and retry "
                        + CHECK_UPDATE_COMMAND
                        + "; do not call the local version the latest.",
                    )
                row = rows[0]
                latest_version, latest_commit = (
                    row["manifest"]["version"],
                    row["commit"],
                )
                available = before.get("commit") != latest_commit
                launcher_update = meta["package"] != str(
                    Path(channel.engine.local)
                    / "packages"
                    / (latest_version + "-" + latest_commit)
                )
                hook = hooks.refresh(home, check_only=True)
                result["global_hook"] = hook
                available = (
                    available
                    or launcher_update
                    or hook["update_available"]
                    or result["tool_discovery"]["registration_update_required"]
                )
                result["launcher_update_available"] = launcher_update
                if channel.verified_active() != before:
                    return dict(result, reason="active-package-changed-during-check")
                result.update(
                    status="client-update-available"
                    if available
                    else "client-update-current",
                    latest_channel_version=latest_version,
                    latest_channel_commit=latest_commit,
                    update_available=available,
                    channel_checked=True,
                    next_action=(
                        "Run " + UPDATE_COMMAND + " to apply this trusted candidate. "
                        if available
                        else "The local active package matches the trusted channel. "
                    )
                    + "The CLI cannot observe the running worker. Confirm MCP client_runtime; a normal host reload starts a new worker when needed.",
                )
                return result
            legacy_bridge = (
                before["version"] == "0.3.0"
                and channel.engine.adapter.legacy_030_https_policy
            )
            if not legacy_bridge and not bootstrap_migrated:
                bootstrap_migrated = windows_launcher_handover(
                    home, channel=channel, before=before, meta=meta
                )
                result["native_changed"] = bootstrap_migrated or hook_changed
            if (
                not bootstrap_migrated
                and before["version"] == "0.3.0"
                and channel.engine.adapter.legacy_030_https_policy
            ):
                # Switch the owned MCP command to the verified candidate
                # bootstrap while the old 0.3.0 package is still active. The
                # new bootstrap accepts that exact historical active package,
                # so an interruption before activation remains serviceable.
                discovered = channel.discover(
                    "codex", policy["platform"], before["version"]
                )
                rows = discovered.get("releases", [])
                if discovered.get("status") != "compatible-update" or len(rows) != 1:
                    return dict(
                        result,
                        status="client-update-review-required",
                        reason="bootstrap-bridge-candidate-unconfirmed",
                    )
                row = rows[0]
                candidate = (
                    channel.engine.local
                    / "packages"
                    / (row["manifest"]["version"] + "-" + row["commit"])
                )
                bridge_plan = content_install.prepare(
                    home,
                    candidate,
                    meta["release_policy"],
                    meta["release_policy_sha256"],
                    replace_existing=True,
                )
                handover_path = safe(
                    Path(home)
                    / ".config/agent-brain-client/bootstrap-handover-plan.json"
                )
                if handover_path.exists():
                    return dict(
                        result, reason="bootstrap-handover-plan-already-present"
                    )
                adapters_storage_private_files.save(handover_path, bridge_plan)
                bridge = content_install.apply(
                    bridge_plan,
                    sha(content_install.contract.encode(bridge_plan)),
                    check=content_install.probe_bootstrap,
                )
                if bridge.get(
                    "status"
                ) != "content-native-client-installed" or not bridge.get("installed"):
                    if bridge.get("status") == "content-install-rolled-back":
                        handover_path.unlink(missing_ok=True)
                    return dict(
                        result,
                        status="client-update-review-required",
                        reason="bootstrap-bridge-transaction-failed",
                        native_changed=bridge.get("native_changed"),
                    )
                # The install transaction has changed the MCP registration even
                # if later channel activation or verification raises.
                bootstrap_migrated = True
                result["native_changed"] = True
                if not content_install.inspect(home).get("registration_verified"):
                    return dict(
                        result,
                        status="client-update-review-required",
                        reason="bootstrap-bridge-verification-failed",
                        native_changed=True,
                    )
                if content_install.sha(
                    handover_path.read_bytes()
                ) != content_install.sha(content_install.contract.encode(bridge_plan)):
                    return dict(
                        result,
                        reason="bootstrap-handover-plan-changed-after-transaction",
                        native_changed=None,
                    )
                handover_path.unlink()
            updated = channel.update()
            after = channel.verified_active()
            if updated.get("status") not in ("current", "updated") or after is None:
                if after is not None:
                    changed = after != before
                    result.update(
                        active_package_version=after["version"],
                        active_package_commit=after.get("commit"),
                        active_package_changed=changed,
                        reload_required=None,
                    )
                else:
                    result.update(
                        active_package_version=None,
                        active_package_commit=None,
                        active_package_changed=None,
                        reload_required=None,
                    )
                return dict(
                    result,
                    status="client-update-unavailable"
                    if updated.get("status") == "offline"
                    else "client-update-review-required",
                    native_changed=bootstrap_migrated,
                    next_action="Update was not confirmed. "
                    + (
                        "A verified active package is retained or restored. "
                        if after is not None
                        else "No active package was verified; preserve recovery material. "
                    )
                    + (
                        "The compatible bootstrap handover is complete and the prior 0.3.0 package remains active. "
                        if bootstrap_migrated
                        and after is not None
                        and after["version"] == "0.3.0"
                        else ""
                    )
                    + "Retry the same public update entry after reviewing the installation/channel state. Do not repeat pairing or import.",
                )
            changed = after != before
            current_meta = private_json(
                safe(Path(home) / ".config/agent-brain-client/codex-mcp.json")
            )
            result["tool_discovery"] = content_install.tool_discovery(
                home, current_meta, after
            )
            launcher = Path(current_meta["package"])
            launcher_manifest = launcher / "verified.json"
            launcher_record = json.loads(
                adapters_storage_private_files.raw(
                    launcher_manifest
                    if launcher_manifest.exists()
                    else launcher / "export-manifest.json"
                )
            )
            result.update(
                registered_launcher_package=current_meta["package"],
                registered_launcher_version=launcher_record.get(
                    "manifest", launcher_record
                ).get("version"),
                registered_tool_count=len(
                    result["tool_discovery"]["registered_enabled_tools"]
                ),
            )
            result.update(
                status="client-update-updated" if changed else "client-update-current",
                active_package_version=after["version"],
                active_package_commit=after.get("commit"),
                latest_channel_version=after["version"],
                latest_channel_commit=after.get("commit"),
                update_available=False,
                channel_checked=True,
                active_package_changed=changed,
                native_changed=bootstrap_migrated,
                reload_required=None,
                next_action="The verified local package is ready; inspect fresh MCP worker evidence before deciding whether a reload is needed. This command does not import real memories.",
            )
            # A current channel may still have an older global Hook command.
            # Update only its executable code, preserving all source decisions.
            try:
                hook = hooks.refresh(home)
            except hooks.ManagedHookConflict as error:
                return config_conflict(result, error.field)
            except HostConfigChanged:
                return config_busy(result)
            except Exception:
                return dict(
                    result,
                    status="client-update-review-required",
                    reason="global-hook-refresh-required",
                    native_changed=None,
                    update_available=None,
                    next_action="The verified package is ready but the global Hook refresh is unconfirmed. Retry the same public update entry; preserve source approval and review the normal host Hook prompt.",
                )
            hook_changed = hook_changed or hook["native_changed"]
            result.update(
                global_hook=hook,
                bootstrap_registration_changed=bootstrap_migrated,
                hook_configuration_changed=hook_changed,
                native_changed=bootstrap_migrated or hook_changed,
                reload_required=None,
            )
            if hook["status"] == "global-hook-updated":
                result["status"] = "client-update-updated"
            return result
        finally:
            os.environ.clear()
            os.environ.update(previous)
    except hooks.ManagedHookConflict as error:
        return config_conflict(result, error.field)
    except HostConfigChanged:
        return config_busy(result)
    except Exception:
        pending = (
            Path(home) / ".config/agent-brain-client/launcher-handover.json"
        ).exists()
        return dict(
            result,
            native_changed=None if pending else bootstrap_migrated or hook_changed,
            reason="launcher-handover-recovery-required"
            if pending
            else "verified-update-or-launcher-required",
        )
