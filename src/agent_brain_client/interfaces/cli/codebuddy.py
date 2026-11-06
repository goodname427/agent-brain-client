"""CodeBuddy host installation, verified stdio gateway and native Hook entry."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    _ENTRY_ROOT = Path(__file__).resolve().parents[4]
    sys.path[:0] = [str(_ENTRY_ROOT / "src"), str(_ENTRY_ROOT)]


import agent_brain_client.runtime.host_binding as host_binding
import agent_brain_client.adapters.storage.bounded_json as bounded_json


import argparse
import hashlib
import json
import os
import subprocess

ROOT = Path(__file__).resolve().parents[4]


def backend_home(home):
    """Metadata-only bootstrap path; works before a Python runtime is prepared."""
    from agent_brain_client.adapters.storage.reinstallation import read_journal

    fresh = read_journal(home)
    if fresh is not None and fresh["phase"] != "complete":
        native = Path(fresh["native"])
        if not native.is_absolute() or native.is_symlink():
            raise ValueError("Original fresh-install native home required")
        return native
    registration = Path(home) / ".config/agent-brain-client/codex-mcp.json"
    if not registration.exists():
        if fresh is not None:
            return Path(fresh["native"])
        return Path(home) / ".config/agent-brain-client/shared-native"
    if registration.is_symlink():
        raise ValueError("Existing Client registration changed")
    metadata = json.loads(registration.read_bytes())
    policy = Path(metadata["release_policy"])
    if (
        policy.is_symlink()
        or hashlib.sha256(policy.read_bytes()).hexdigest()
        != metadata["release_policy_sha256"]
    ):
        raise ValueError("Existing Client release policy changed")
    native = Path(json.loads(policy.read_bytes())["runtime"]["codex_home"])
    if not native.is_absolute() or native.is_symlink():
        raise ValueError("Existing shared native binding required")
    return native


def verify_adapter():
    pin = ROOT / "code.json"
    if not pin.exists():
        return  # Source checkout is only an installer/developer entry.
    if pin.is_symlink() or pin.stat().st_size > 65536:
        raise ValueError("Frozen CodeBuddy code metadata changed")
    value = json.loads(pin.read_bytes())
    from agent_brain_client.adapters.packages.layout import PYTHON_FILES

    names = set(PYTHON_FILES)
    if value.get("schema") != 1 or set(value.get("files", {})) != names:
        raise ValueError("Frozen CodeBuddy code scope changed")
    for name, sha in value["files"].items():
        path = ROOT / name
        if (
            any(p.is_symlink() for p in (path, *path.parents))
            or hashlib.sha256(path.read_bytes()).hexdigest() != sha
        ):
            raise ValueError("Frozen CodeBuddy code changed")


def probe(home, core, runner=subprocess.run):
    from agent_brain_client.runtime.host_binding import backend

    command, _ = backend(home, core)
    result = runner(
        [command["command"], *command["args"], "--installation-check"],
        env=dict(os.environ, **command.get("env", {})),
        capture_output=True,
        timeout=120,
    )
    if result.returncode or len(result.stdout) > 16384:
        raise ValueError("Shared Client protocol check unconfirmed")
    value = json.loads(result.stdout)
    if (
        value.get("protocol_checked") is not True
        or value.get("authentication_checked") is not True
    ):
        raise ValueError("Shared Client protocol check unconfirmed")
    return {
        "status": "codebuddy-backend-protocol-checked",
        "protocol_checked": True,
        "model_loaded": False,
    }


def main():
    if sys.argv[1:] == ["backend-home"]:
        print(backend_home(Path.home()))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("install", "status", "uninstall", "probe", "serve", "hook")
    )
    parser.add_argument("--surface", choices=("ide", "cli", "both"), default="both")
    parser.add_argument("--device-home", type=Path, default=Path.home())
    args = parser.parse_args()
    try:
        verify_adapter()
        import agent_brain_client.adapters.harnesses.codebuddy.events as runtime

        home = bounded_json.canonical(args.device_home)
        core = host_binding.load_core(home)
        from agent_brain_client.adapters.platforms.filesystem import require_platform

        require_platform("codebuddy-host")
        if args.action == "serve":
            host_binding.execute_mcp(home, core)
            return 0
        if args.action == "hook":
            output, state = runtime.hook(home, sys.stdin.buffer, core=core)
            print(json.dumps(output, ensure_ascii=False))
            # Keep diagnostics content-free. A pending queue is not a save.
            diagnostic = {
                key: state[key]
                for key in (
                    "status",
                    "error_type",
                    "recall_status",
                    "recall_error_type",
                    "assistant_capture",
                )
                if key in state
            }
            if state.get("error"):
                diagnostic["capture_error"] = state["error"].get("code", "unconfirmed")
            print(
                json.dumps(dict(operation="codebuddy-hook", **diagnostic)),
                file=sys.stderr,
            )
            return 0
        import agent_brain_client.adapters.harnesses.codebuddy.configuration as host

        if args.action == "probe":
            value = probe(home, core)
        elif args.action == "status":
            value = host.status(home, core, backend_check=host_binding.backend)
            value["source_enabled"] = core.approved(core.load(home))
        else:
            if args.action == "install":
                checked = probe(home, core)
            value = host.install(
                home,
                ROOT,
                core,
                surface=args.surface,
                remove=args.action == "uninstall",
            )
            if args.action == "install":
                value.update(checked)
                value["status"] = "codebuddy-configured"
                value["source_enabled"] = core.approved(core.load(home))
        print(json.dumps(value, ensure_ascii=False))
        return 0
    except Exception as error:
        if args.action == "hook":
            print("{}")
        print(
            json.dumps(
                {
                    "status": "codebuddy-review-or-resume-required",
                    "operation": args.action,
                    "error_type": type(error).__name__,
                    "preserve_existing_state": True,
                    "next_action": "Check the existing shared Client installation and CodeBuddy owned configuration; preserve credentials, queues and recovery receipts.",
                }
            ),
            file=sys.stderr if args.action in ("hook", "serve") else sys.stdout,
        )
        return 0 if args.action == "hook" else 2


if __name__ == "__main__":
    sys.exit(main())
