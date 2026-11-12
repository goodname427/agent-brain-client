"""Client maintenance decisions and recovery sequencing through installation ports."""

from agent_brain_client.application.installation.update_ports import (
    UpdateInstallation,
    ManagedConfigurationConflict,
    HostEditInProgress,
)


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


class ClientUpdate:
    def __init__(self, installation: UpdateInstallation):
        self.installation = installation

    def run(self, action):
        result = self._run(action)
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
                    "Run "
                    + self.installation.update_command
                    + " to apply the trusted candidate. "
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
            try:
                if (
                    not self.installation.collection_installed()
                    or result.get("global_hook", {}).get("status")
                    == "global-hook-not-installed"
                ):
                    result["global_hook"] = self.installation.install_collection()
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
                result["source_setup"] = self.installation.setup_sources()
            except Exception:
                result["source_setup"] = {
                    "status": "global-memory-connection-pending",
                    "collection_enabled": False,
                    "next_action": "Retry the same update entry; existing configuration is retained.",
                }
        return result

    def _run(self, action):
        """Reuse the registered updater; no pairing or worker restart.

        Discovery may cache verified candidates, but never changes the active pointer.
        The CLI cannot observe host worker versions. MCP responses report their own.
        """
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
            if self.installation.recover_hooks(check_only=action == "check-update"):
                if action == "check-update":
                    return dict(
                        result,
                        reason="global-hook-recovery-required",
                        native_changed=None,
                    )
                hook_changed = True
                result["native_changed"] = True
            if self.installation.handover_pending():
                if action != "update":
                    return dict(
                        result,
                        reason="launcher-handover-recovery-required",
                        native_changed=None,
                    )
                launcher_changed = self.installation.refresh_launcher()
                result["native_changed"] = launcher_changed
            state = self.installation.inspect()
            if not state.get("registration_verified") or state.get(
                "installation_state"
            ) not in ("verified", "verified-disabled"):
                if state.get("reason") == "mcp-binding-changed":
                    return config_conflict(
                        result, self.installation.mcp_config_field, state["reason"]
                    )
                return dict(
                    result, reason=state.get("reason", "verified-installation-required")
                )
            registration = self.installation.load_registration()
            result.update(registration.launcher)
            if not registration.supported:
                return dict(result, reason="existing-https-installation-required")
            with self.installation.environment():
                channel = self.installation.open_channel()
                before = channel.verified_active()
                if before is None:
                    return dict(result, reason="verified-active-package-required")
                result.update(
                    active_package_version=before["version"],
                    active_package_commit=before.get("commit"),
                )
                result["tool_discovery"] = self.installation.tool_discovery(before)
                if action == "check-update":
                    discovered = channel.discover(
                        registration.transport_adapter, registration.platform, before["version"]
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
                            + self.installation.check_command
                            + "; do not call the local version the latest.",
                        )
                    row = rows[0]
                    latest_version, latest_commit = (
                        row["manifest"]["version"],
                        row["commit"],
                    )
                    available = before.get("commit") != latest_commit
                    launcher_update = self.installation.launcher_update_available(
                        latest_version, latest_commit
                    )
                    hook = self.installation.refresh_hooks(check_only=True)
                    result["global_hook"] = hook
                    available = (
                        available
                        or launcher_update
                        or hook["update_available"]
                        or result["tool_discovery"]["registration_update_required"]
                    )
                    result["launcher_update_available"] = launcher_update
                    if channel.verified_active() != before:
                        return dict(
                            result, reason="active-package-changed-during-check"
                        )
                    result.update(
                        status="client-update-available"
                        if available
                        else "client-update-current",
                        latest_channel_version=latest_version,
                        latest_channel_commit=latest_commit,
                        update_available=available,
                        channel_checked=True,
                        next_action=(
                            "Run "
                            + self.installation.update_command
                            + " to apply this trusted candidate. "
                            if available
                            else "The local active package matches the trusted channel. "
                        )
                        + "The CLI cannot observe the running worker. Confirm MCP client_runtime; a normal host reload starts a new worker when needed.",
                    )
                    return result
                if not launcher_changed:
                    launcher_changed = self.installation.refresh_launcher(
                        channel=channel, before=before
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
                registration = self.installation.load_registration()
                result["tool_discovery"] = self.installation.tool_discovery(after)
                result.update(registration.launcher)
                result["registered_tool_count"] = len(
                    result["tool_discovery"]["registered_enabled_tools"]
                )
                result.update(
                    status="client-update-updated"
                    if changed
                    else "client-update-current",
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
                    hook = self.installation.refresh_hooks()
                except ManagedConfigurationConflict as error:
                    return config_conflict(result, error.field)
                except HostEditInProgress:
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
        except ManagedConfigurationConflict as error:
            return config_conflict(result, error.field)
        except HostEditInProgress:
            return config_busy(result)
        except Exception:
            pending = self.installation.handover_pending()
            return dict(
                result,
                native_changed=None if pending else launcher_changed or hook_changed,
                reason="launcher-handover-recovery-required"
                if pending
                else "verified-update-or-launcher-required",
            )
