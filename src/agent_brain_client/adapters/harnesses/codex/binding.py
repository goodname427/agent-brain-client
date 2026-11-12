"""Publish the Codex MCP entry from an authenticated shared service command."""

import copy
import json
import os
from pathlib import Path
import tomllib

from agent_brain_client.adapters.storage import metadata as io
from agent_brain_client.adapters.storage.host_files import host_image, host_plan, host_apply, host_restore
from agent_brain_client.adapters.storage.toml_document import toml_replace, toml_remove
from agent_brain_client.adapters.platforms.filesystem import private_directory, locked

NAME = "agent_brain_server"


def directory(home):
    return io.canonical(Path(home) / ".config/agent-brain-client/codex")


def private_control(home, runtime):
    return Path(runtime["codex_home"]) == Path(home) / ".config/agent-brain-client/shared-native"


def receipt(home):
    file = directory(home) / "registration.json"
    if not file.exists():
        return None
    value = io.private_json(file)
    if (set(value) != {"schema", "native_home", "mcp", "removed"}
            or value["schema"] != 1 or type(value["removed"]) is not bool
            or not isinstance(value["mcp"], dict)):
        raise ValueError("Exact Codex native registration required")
    native = io.canonical(Path(value["native_home"]))
    if not native.is_absolute() or native == directory(home).parent / "shared-native":
        raise ValueError("Separate Codex native home required")
    return value


def native_home(home, runtime):
    if not private_control(home, runtime):
        return io.canonical(Path(runtime["codex_home"]))  # Authenticated retained data.
    value = receipt(home)
    if value is None:
        raise ValueError("Explicit Codex native binding required")
    return io.canonical(Path(value["native_home"]))


def location(home, runtime):
    native = native_home(home, runtime)
    cache = (directory(home) / "content-cache" if private_control(home, runtime)
             else Path(runtime["content_cache"]))
    return native, cache


def entry(raw):
    return tomllib.loads(raw.decode()).get("mcp_servers", {}).get(NAME)


def matches(actual, expected):
    return actual == expected or (isinstance(actual, dict) and expected.get("enabled") is True
                                 and actual == dict(expected, enabled=False))


def read(file):
    image = host_image(file)
    return image[0] if image else b""


def recover(home):
    file = directory(home) / "transaction.json"
    if not file.exists():
        return False
    value = io.private_json(file)
    native = io.canonical(Path(value["native_home"]))
    permitted = {str(native / "config.toml"), str(directory(home) / "registration.json")}
    if value.get("schema") != 1 or set(value.get("host", {})) != permitted:
        raise ValueError("Codex transaction scope changed")
    if value.get("phase") != "applying":
        return False
    host_restore(value["host"], lambda: io.write(file, value))
    io.write(file, dict(value, phase="rolled-back"))
    return True


def install(home, *, native=None, remove=False, enabled=None, check_only=False):
    from agent_brain_client.adapters.storage.registration import registration
    from agent_brain_client.adapters.installation.installation import registered_context
    meta, policy = registration(home)
    if not private_control(home, policy["runtime"]):
        return {"status": "retained-direct-codex-binding", "native_changed": False,
                "update_available": False}
    state = directory(home)
    private_directory(state)
    with locked(state / "binding.lock"):
        recover(home)
        previous = receipt(home)
        if remove and (previous is None or previous["removed"]):
            return {"status": "codex-uninstalled", "native_changed": False, "update_available": False}
        if previous is not None and previous["removed"]:
            if native is None:
                return {"status": "codex-uninstalled", "native_changed": False, "update_available": False}
            previous = None
        target = io.canonical(Path(previous["native_home"]) if previous else Path(
            native if native is not None else os.environ.get("CODEX_HOME", str(Path(home) / ".codex"))))
        if target == Path(policy["runtime"]["codex_home"]):
            raise ValueError("Codex native and shared control homes must differ")
        if previous and native is not None and target != io.canonical(Path(native)):
            raise ValueError("Original Codex native home required")
        config = target / "config.toml"
        before = read(config)
        actual = entry(before)
        if actual is not None and (previous is None or not matches(actual, previous["mcp"])):
            raise ValueError("Existing Codex MCP entry preserved")
        if previous and not matches(actual, previous["mcp"]):
            raise ValueError("Owned Codex MCP entry changed")
        _, command, _, _ = registered_context(home, meta)
        wanted = copy.deepcopy(command)
        argv = wanted["args"]
        argv[argv.index("--harness") + 1] = "codex"
        wanted.setdefault("env", {})["CODEX_HOME"] = str(target)
        wanted["enabled"] = enabled if enabled is not None else (actual.get("enabled", True) if actual else True)
        if remove:
            from agent_brain_client.adapters.harnesses.codex import collection_installation as hooks
            if hooks.split_owned(before)[1] is not None:
                from types import SimpleNamespace
                hooks.uninstall(SimpleNamespace(device_home=str(home)))
                before = read(config)
                if not matches(entry(before), previous["mcp"]):
                    raise ValueError("Owned Codex MCP entry changed during Hook removal")
        before_hooks = before
        after = (toml_remove(before_hooks.decode(), [("mcp_servers", NAME)]) if remove else
                 toml_replace(before.decode(), ("mcp_servers", NAME), wanted)).encode()
        saved = {"schema": 1, "native_home": str(target), "mcp": wanted, "removed": remove}
        available = before != after or saved != previous
        result = {"status": "codex-uninstalled" if remove else "codex-configured",
                  "native_changed": False, "update_available": available,
                  "service_enabled": wanted["enabled"] and not remove}
        if check_only or not available:
            return result
        if not target.exists():
            private_directory(target)
        file = state / "registration.json"
        payload = {config: after, file: (json.dumps(saved, sort_keys=True, indent=2) + "\n").encode()}
        record = {"schema": 1, "phase": "applying", "native_home": str(target),
                  "host": host_plan(payload, private_paths=(file,))}
        journal = state / "transaction.json"
        io.write(journal, record)
        try:
            host_apply(record["host"], lambda: io.write(journal, record))
            io.write(journal, dict(record, phase="complete"))
        except Exception:
            host_restore(record["host"], lambda: io.write(journal, record))
            io.write(journal, dict(record, phase="rolled-back"))
            raise
        return dict(result, native_changed=True, update_available=False)


def detach_payload(home):
    value = receipt(home)
    if value is None or value["removed"]:
        return {}
    file = Path(value["native_home"]) / "config.toml"
    before = read(file)
    if not matches(entry(before), value["mcp"]):
        raise ValueError("Owned Codex MCP entry changed")
    # Hooks are independently receipt-owned and share the same native document.
    from agent_brain_client.adapters.harnesses.codex.content_ports import CodexContentPorts
    outside, _ = CodexContentPorts.remove_hooks(home, before, None)
    return {file: toml_remove(outside.decode(), [("mcp_servers", NAME)]).encode()}


def status(home, runtime):
    native = native_home(home, runtime)
    value = receipt(home)
    actual = entry(read(native / "config.toml"))
    if value is not None and value["removed"]:
        if actual is not None:
            raise ValueError("Removed Codex MCP entry changed")
        return {"installed": False, "service_enabled": False}
    if value is not None and not matches(actual, value["mcp"]):
        raise ValueError("Owned Codex MCP entry changed")
    return {"installed": actual is not None, "service_enabled": actual is not None and actual.get("enabled", True)}
