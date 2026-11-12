"""Compose Client maintenance ports for the public installation interface."""

import json
import subprocess
import sys
from agent_brain_client.application.installation.update import ClientUpdate
from agent_brain_client.application.installation.update_ports import HostEditInProgress
from agent_brain_client.adapters.harnesses.codex.updates import CodexUpdateInstallation
from agent_brain_client.adapters.harnesses.codex import update_interpreter


class HostUpdateInstallation:
    """Compose independent host transactions behind the maintenance port.

    The shared installation keeps its own verified update path. This coordinator
    adds only already registered hosts; these transactions are not cross-host atomic.
    """

    def __init__(self, home, installation):
        self.home = home
        self.installation = installation
        self.mcp_config_field = installation.mcp_config_field
        self.check_command = installation.check_command
        self.update_command = installation.update_command

    def recover_hooks(self, *, check_only):
        return self.installation.recover_hooks(check_only=check_only)

    def handover_pending(self):
        return self.installation.handover_pending()

    def refresh_launcher(self, *, channel=None, before=None):
        return self.installation.refresh_launcher(channel=channel, before=before)

    def inspect(self):
        return self.installation.inspect()

    def load_registration(self):
        return self.installation.load_registration()

    def environment(self):
        return self.installation.environment()

    def open_channel(self):
        self.channel = self.installation.open_channel()
        return self.channel

    def tool_discovery(self, active):
        return self.installation.tool_discovery(active)

    def launcher_update_available(self, version, commit):
        return self.installation.launcher_update_available(version, commit)

    def refresh_hooks(self, *, check_only=False):
        from agent_brain_client.adapters.storage.private_files import safe

        active = self.channel.verified_active()
        if active is None:
            raise ValueError("Verified Harness package required")
        active = dict(active)
        package = safe(active["package"])
        result = self.installation.refresh_hooks(check_only=check_only)
        if self.channel.verified_active() != active:
            raise ValueError("Active package changed before Harness refresh")
        binding = refresh_bindings(self.home, package, check_only=check_only)
        if self.channel.verified_active() != active:
            raise ValueError("Active package Harness refresh unconfirmed")
        result = dict(result, codebuddy=binding)
        result["update_available"] = result["update_available"] or binding["update_available"]
        result["native_changed"] = result["native_changed"] or binding["native_changed"]
        if binding["native_changed"]:
            result["status"] = "global-hook-updated"
        return result

    def collection_installed(self):
        return self.installation.collection_installed()

    def install_collection(self):
        return self.installation.install_collection()

    def setup_sources(self):
        return self.installation.setup_sources()


def manage_update(home, action, *, channel_factory=None):
    from agent_brain_client.runtime.installation_completion import source_setup

    return ClientUpdate(
        HostUpdateInstallation(
            home,
            CodexUpdateInstallation(
                home,
                channel_factory=channel_factory,
                setup_sources=lambda: source_setup(home),
            ),
        )
    ).run(action)


def reexec_update_runtime(home, action, *, runner=subprocess.run):
    return update_interpreter.reexec_update_runtime(home, action, runner=runner)


def refresh_bindings(home, package, *, check_only=False):
    """Use the verified package's renderer as well as its frozen source bytes."""
    code = """
import json, pathlib, sys
package = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(package / "src"))
from agent_brain_client.adapters.harnesses.codebuddy import configuration
from agent_brain_client.adapters.storage.host_files import HostConfigChanged
refresh = getattr(configuration, "refresh", None)
try:
    result = (refresh(sys.argv[2], package, check_only=sys.argv[3] == "check") if refresh else
              {"status": "codebuddy-refresh-requires-new-package", "native_changed": False,
               "update_available": False})
    value = {"result": result}
except HostConfigChanged:
    value = {"host_edit_in_progress": True}
print(json.dumps(value))
"""
    run = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(package), str(home),
         "check" if check_only else "update"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
    )
    if run.returncode or len(run.stdout) > 16384:
        raise ValueError("Verified package CodeBuddy refresh unconfirmed")
    value = json.loads(run.stdout)
    if value == {"host_edit_in_progress": True}:
        raise HostEditInProgress("Verified package CodeBuddy configuration is busy")
    if not isinstance(value, dict) or set(value) != {"result"}:
        raise ValueError("Invalid package CodeBuddy refresh response")
    result = value["result"]
    if (not isinstance(result, dict)
            or type(result.get("native_changed")) is not bool
            or type(result.get("update_available")) is not bool):
        raise ValueError("Invalid package CodeBuddy refresh response")
    return result
