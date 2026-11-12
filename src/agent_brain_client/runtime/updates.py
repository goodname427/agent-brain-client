"""Compose Client maintenance ports for the public installation interface."""

import json
import subprocess
import sys
from agent_brain_client.application.installation.update import ClientUpdate
from agent_brain_client.adapters.harnesses.codex.updates import CodexUpdateInstallation
from agent_brain_client.adapters.harnesses.codex import update_interpreter


def manage_update(home, action, *, channel_factory=None):
    from agent_brain_client.runtime.installation_completion import source_setup

    return ClientUpdate(
        CodexUpdateInstallation(
            home,
            channel_factory=channel_factory,
            setup_sources=lambda: source_setup(home),
            refresh_bindings=lambda package, **kwargs: refresh_bindings(home, package, **kwargs),
        )
    ).run(action)


def reexec_update_runtime(home, action, *, runner=subprocess.run):
    return update_interpreter.reexec_update_runtime(home, action, runner=runner)


def refresh_bindings(home, package, *, check_only=False, same_code=False):
    """Use the verified package's renderer as well as its frozen source bytes."""
    if same_code:
        from agent_brain_client.adapters.harnesses.codebuddy.configuration import refresh

        return refresh(home, package, check_only=check_only)
    code = """
import json, pathlib, sys
package = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(package / "src"))
from agent_brain_client.adapters.harnesses.codebuddy import configuration
refresh = getattr(configuration, "refresh", None)
result = (refresh(sys.argv[2], package, check_only=sys.argv[3] == "check") if refresh else
          {"status": "codebuddy-refresh-requires-new-package", "native_changed": False,
           "update_available": False})
print(json.dumps(result))
"""
    run = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(package), str(home),
         "check" if check_only else "update"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
    )
    if run.returncode or len(run.stdout) > 16384:
        raise ValueError("Verified package CodeBuddy refresh unconfirmed")
    result = json.loads(run.stdout)
    if (not isinstance(result, dict)
            or type(result.get("native_changed")) is not bool
            or type(result.get("update_available")) is not bool):
        raise ValueError("Invalid package CodeBuddy refresh response")
    return result
