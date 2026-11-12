"""Compose Client maintenance ports for the public installation interface."""

import json
import subprocess
import sys
from agent_brain_client.application.installation.update import ClientUpdate
from agent_brain_client.application.installation.update_ports import HostEditInProgress
from agent_brain_client.adapters.installation.updates import RegisteredClientInstallation
from agent_brain_client.adapters.installation import update_interpreter


def manage_update(home, action, *, channel_factory=None):
    from agent_brain_client.runtime.installation_completion import source_setup

    from agent_brain_client.runtime.host_maintenance import HostMaintenance
    return ClientUpdate(
        RegisteredClientInstallation(
                home,
                channel_factory=channel_factory,
                setup_sources=lambda: source_setup(home),
                host_hooks=HostMaintenance(home),
        )
    ).run(action)


def reexec_update_runtime(home, action, *, runner=subprocess.run):
    from agent_brain_client.runtime.host_maintenance import HostMaintenance
    return update_interpreter.reexec_update_runtime(home, action, recovery_context=HostMaintenance(home).registration_preimage, runner=runner)


def refresh_bindings(home, package, *, harness, check_only=False):
    """Use the verified package's renderer as well as its frozen source bytes."""
    if harness not in ("codex", "codebuddy"):
        raise ValueError("Explicit maintenance Harness required")
    code = """
import json, pathlib, sys
package = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(package / "src"))
if sys.argv[4] == "codebuddy":
    from agent_brain_client.adapters.harnesses.codebuddy import configuration
elif sys.argv[4] == "codex":
    from agent_brain_client.adapters.harnesses.codex import collection_installation as configuration
else:
    raise ValueError("Explicit maintenance Harness required")
from agent_brain_client.adapters.storage.host_files import HostConfigChanged
from agent_brain_client.adapters.storage.collection_setup import refresh_code
refresh = configuration.refresh
try:
    result = (refresh(sys.argv[2], package, check_only=sys.argv[3] == "check") if sys.argv[4] == "codebuddy" else
              refresh(sys.argv[2], check_only=sys.argv[3] == "check"))
    if sys.argv[4] == "codebuddy":
        code_changed = refresh_code(pathlib.Path(sys.argv[2]), package, check_only=sys.argv[3] == "check")
        result = dict(result, update_available=result["update_available"] or (code_changed and sys.argv[3] == "check"))
    value = {"result": result}
except HostConfigChanged:
    value = {"host_edit_in_progress": True}
except ValueError as error:
    if hasattr(error, "field"):
        value = {"conflict_field": error.field}
    else:
        raise
print(json.dumps(value))
"""
    run = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(package), str(home),
         "check" if check_only else "update", harness],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
    )
    if run.returncode or len(run.stdout) > 16384:
        raise ValueError("Verified package CodeBuddy refresh unconfirmed")
    value = json.loads(run.stdout)
    if set(value) == {"conflict_field"}:
        from agent_brain_client.application.installation.update_ports import ManagedConfigurationConflict
        raise ManagedConfigurationConflict(value["conflict_field"])
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
