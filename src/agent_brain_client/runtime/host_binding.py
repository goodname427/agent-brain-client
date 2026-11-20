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
from agent_brain_client.adapters.packages.frozen import (
    compatible_codebuddy_runtime,
    frozen_source_files,
    runtime_pins,
    validate_python_pins,
)


def load_core(home):
    """Use the existing immutable global runtime, including its own code pins.

    The gateway and shared runtime must contain the same reviewed Hook code.
    An immutable CodeBuddy snapshot may differ in compatible MCP worker files.
    """
    directory = canonical(Path(home) / ".config/agent-brain-client/global-memory")
    value = read_json(directory / "settings.json")
    if value.get("device_home") != str(Path(home)):
        raise ValueError("Existing Client device binding required")
    pins = value.get("code_sha256")
    if not isinstance(pins, dict) or not 1 <= len(pins) <= 256:
        raise ValueError("Existing frozen Client runtime required")
    validate_python_pins(pins)
    executing_root = Path(__file__).resolve().parents[3]
    executing_pins = runtime_pins(executing_root)
    root = canonical(
        directory / "code" / digest(json.dumps(pins, sort_keys=True).encode())
    )
    if pins != executing_pins and not compatible_codebuddy_runtime(
        home, executing_root, executing_pins, root, pins
    ):
        raise ValueError("Refresh the Harness binding to the current Client runtime")
    if (pins.keys() - set(PYTHON_FILES) or (root / "code.json").exists()) and (
        runtime_pins(root) != pins or frozen_source_files(root) != pins.keys()
    ):
        raise ValueError("Frozen Client membership changed")
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
        executing = canonical(executing_root / name)
        if not executing.is_file() or digest(executing.read_bytes()) != executing_pins[name]:
            raise ValueError(
                "Refresh the Harness binding to the current Client runtime"
            )
    import agent_brain_client.runtime.collection as core

    platform_files.private_mode(directory, directory=True, owner=True)
    for name in pins:
        platform_files.private_mode(root / name, owner=True)
    return core


def inspect_backend(home, core):
    """Verify registration metadata while keeping a paused service paused."""
    from agent_brain_client.adapters.installation.installation import inspect, registered_context
    status = inspect(home)
    if status.get("registration_verified", status.get("installed")) is not True:
        raise ValueError("Verified shared Client registration required")
    metadata, _ = registration_store.registration(home)
    _, command, _, _ = registered_context(home, metadata)
    return command, status


def backend(home, core):
    command, status = inspect_backend(home, core)
    if status.get("installed") is not True:
        raise ValueError("Shared Client service is paused")
    return command, status


def execute_mcp(home, core, execute=os.execve, runner=subprocess.run):
    command, _ = backend(home, core)
    argv = [command["command"], *command["args"], "--harness", "codebuddy"]
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
