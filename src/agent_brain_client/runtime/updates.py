"""runtime / updates: concrete installation responsibility."""

import agent_brain_client.adapters.storage.private_files as private_files


import sys
from pathlib import Path
import json
import os
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
from agent_brain_client.runtime.launcher_update import refresh_launcher

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

                from agent_brain_client.adapters.storage.registration import (
                    registration,
                )

                _, hook_policy = registration(home)
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
            Path(home) / ".config/agent-brain-client/launcher-handover.json"
        )
        if not handover.exists():
            return None
        try:
            saved = private_files.private_json(handover)
            plan = saved["plan"]
            plan_digest = content_install.sha(content_install.contract.encode(plan))
            content_install.validate_install_scope(plan)
            state = Path(home) / ".config/agent-brain-client"
            receipt = private_files.private_json(
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
            dependencies = private_files.private_json(
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
    launcher_changed = False
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
            launcher_changed = refresh_launcher(home, channel_factory=channel_factory)
            result["native_changed"] = launcher_changed
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
            private_files.raw(
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
            if not launcher_changed:
                launcher_changed = refresh_launcher(
                    home, channel=channel, before=before, meta=meta
                )
                result["native_changed"] = launcher_changed or hook_changed
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
                    native_changed=launcher_changed,
                    next_action="Update was not confirmed. "
                    + (
                        "A verified active package is retained or restored. "
                        if after is not None
                        else "No active package was verified; preserve recovery material. "
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
                private_files.raw(
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
                native_changed=launcher_changed,
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
                bootstrap_registration_changed=launcher_changed,
                hook_configuration_changed=hook_changed,
                native_changed=launcher_changed or hook_changed,
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
            native_changed=None if pending else launcher_changed or hook_changed,
            reason="launcher-handover-recovery-required"
            if pending
            else "verified-update-or-launcher-required",
        )
