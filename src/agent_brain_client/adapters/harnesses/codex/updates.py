"""Codex maintenance I/O: owned registration, package channel and hook transactions."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace

from agent_brain_client.application.installation.update_ports import (
    RegisteredInstallation,
    ManagedConfigurationConflict,
    HostEditInProgress,
)
from agent_brain_client.adapters.harnesses.codex import (
    installation,
    collection_installation as hooks,
)
from agent_brain_client.adapters.harnesses.codex.launcher_update import refresh_launcher
from agent_brain_client.adapters.packages.client import (
    ClientReleases,
    HTTPS_CONTENT_CAPABILITIES,
    dependency_environment,
    CHECK_UPDATE_COMMAND,
    UPDATE_COMMAND,
)
from agent_brain_client.adapters.storage import private_files
from agent_brain_client.adapters.storage.private_files import safe, private_json
from agent_brain_client.adapters.storage.collection import settings_path
from agent_brain_client.adapters.storage.host_files import HostConfigChanged


class CodexUpdateInstallation:
    mcp_config_field = "mcp_servers." + installation.NAME
    check_command = CHECK_UPDATE_COMMAND
    update_command = UPDATE_COMMAND

    def __init__(self, home, *, channel_factory=None, setup_sources):
        self.home = Path(home)
        self.channel_factory = channel_factory
        self._setup_sources = setup_sources

    @contextmanager
    def config_operation(self):
        try:
            yield
        except hooks.ManagedHookConflict as error:
            raise ManagedConfigurationConflict(error.field) from None
        except HostConfigChanged:
            raise HostEditInProgress() from None

    def recover_hooks(self, *, check_only):
        with self.config_operation():
            return hooks.recover_update(self.home, check_only=check_only)

    def handover_pending(self):
        return safe(
            self.home / ".config/agent-brain-client/launcher-handover.json"
        ).exists()

    def refresh_launcher(self, *, channel=None, before=None):
        with self.config_operation():
            if channel is None:
                return refresh_launcher(self.home, channel_factory=self.channel_factory)
            return refresh_launcher(
                self.home, channel=channel, before=before, meta=self.meta
            )

    def inspect(self):
        return installation.inspect(self.home)

    def load_registration(self):
        self.meta = private_json(
            safe(self.home / ".config/agent-brain-client/codex-mcp.json")
        )
        self.policy, mcp, _, _ = installation.registered_context(self.home, self.meta)
        launcher = Path(self.meta["package"])
        manifest = launcher / "verified.json"
        record = json.loads(
            private_files.raw(
                manifest if manifest.exists() else launcher / "export-manifest.json"
            )
        )
        return RegisteredInstallation(
            self.policy["platform"],
            self.policy["capabilities"] == HTTPS_CONTENT_CAPABILITIES,
            {
                "registered_launcher_package": self.meta["package"],
                "registered_launcher_version": record.get("manifest", record).get(
                    "version"
                ),
                "registered_tool_count": len(mcp["enabled_tools"]),
            },
        )

    @contextmanager
    def environment(self):
        environment = dependency_environment(self.policy)
        previous = dict(os.environ)
        try:
            if environment is not None:
                os.environ.clear()
                os.environ.update(environment, HOME=str(self.home), LANG="en_US.UTF-8")
            yield
        finally:
            os.environ.clear()
            os.environ.update(previous)

    def open_channel(self):
        self.channel = (
            self.channel_factory(
                self.meta["release_policy"], self.meta["release_policy_sha256"]
            )
            if self.channel_factory
            else ClientReleases(
                self.meta["release_policy"],
                self.meta["release_policy_sha256"],
                discovery_timeout=15,
            )
        )
        return self.channel

    def tool_discovery(self, active):
        return installation.tool_discovery(self.home, self.meta, active)

    def launcher_update_available(self, version, commit):
        return self.meta["package"] != str(
            Path(self.channel.engine.local) / "packages" / (version + "-" + commit)
        )

    def refresh_hooks(self, *, check_only=False):
        with self.config_operation():
            return (
                hooks.refresh(self.home, check_only=True)
                if check_only
                else hooks.refresh(self.home)
            )

    def collection_installed(self):
        return settings_path(self.home).exists()

    def install_collection(self):
        return hooks.install(
            SimpleNamespace(
                device_home=str(self.home),
                profile_id=None,
                operator=None,
                operator_sha256=None,
            )
        )

    def setup_sources(self):
        return self._setup_sources()
