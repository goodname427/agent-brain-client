"""Bind a Harness entry to a verified enrolled Client runtime."""

from agent_brain_client.adapters.platforms import filesystem as platform_files
from agent_brain_client.adapters.storage import registration as registration_store
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from agent_brain_client.adapters.storage.bounded_json import (
    canonical,
    digest,
    read_json,
)
from agent_brain_client.adapters.packages.layout import PYTHON_FILES


def load_core(home):
    """Use the existing immutable global runtime, including its own code pins.

    The gateway and shared runtime must contain the same reviewed code. Refresh
    the Harness binding after a Client change; retain policy, credentials and scope.
    """
    directory = canonical(Path(home) / ".config/agent-brain-client/global-memory")
    value = read_json(directory / "settings.json")
    if value.get("device_home") != str(Path(home)):
        raise ValueError("Existing Client device binding required")
    pins = value.get("code_sha256")
    if not isinstance(pins, dict) or not 1 <= len(pins) <= 256:
        raise ValueError("Existing frozen Client runtime required")
    if set(pins) != set(PYTHON_FILES):
        raise ValueError(
            "Current Client runtime required; reinstall before binding a Harness"
        )
    root = canonical(
        directory / "code" / digest(json.dumps(pins, sort_keys=True).encode())
    )
    for name, pin in pins.items():
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or len(path.parts) < 2
            or path.parts[:2] != ("src", "agent_brain_client")
            or not isinstance(pin, str)
            or not re.fullmatch("[a-f0-9]{64}", pin)
        ):
            raise ValueError("Existing Client code scope required")
        file = canonical(root / name)
        if (
            not file.is_file()
            or file.stat().st_nlink != 1
            or digest(file.read_bytes()) != pin
        ):
            raise ValueError("Frozen Client code changed")
        executing = canonical(Path(__file__).resolve().parents[3] / name)
        if not executing.is_file() or digest(executing.read_bytes()) != pin:
            raise ValueError(
                "Refresh the Harness binding to the current Client runtime"
            )
    import agent_brain_client.runtime.collection as core

    platform_files.private_mode(directory, directory=True, owner=True)
    for name in pins:
        platform_files.private_mode(root / name, owner=True)
    return core


def backend(home, core):
    """Authenticate the existing native registration before using its command."""
    from agent_brain_client.adapters.harnesses.codex.installation import (
        inspect,
        registered_context,
    )

    status = inspect(home)
    if status.get("installed") is not True:
        raise ValueError("Verified shared Client installation required")
    metadata, _ = registration_store.registration(home)
    _, command, _, _ = registered_context(home, metadata)
    return command, status


def execute_mcp(home, core, execute=os.execve, runner=subprocess.run):
    command, _ = backend(home, core)
    argv = [command["command"], *command["args"]]
    environment = dict(os.environ)
    environment.update(command.get("env", {}))
    # Keep the verified backend's complete tool set and MCP framing. The
    # enrolled device adapter is transport metadata, not CodeBuddy's host name.
    # Windows execve emulates replacement through the CRT and can terminate
    # before the stdio handshake. Inherit all three streams without buffering.
    if sys.platform == "win32":
        return runner(argv, env=environment, check=False).returncode
    execute(argv[0], argv, environment)
    return 0
