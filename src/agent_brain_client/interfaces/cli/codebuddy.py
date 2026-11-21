"""CodeBuddy host installation, verified stdio gateway and native Hook entry."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    _ENTRY_ROOT = Path(__file__).resolve().parents[4]
    sys.path[:0] = [str(_ENTRY_ROOT / "src"), str(_ENTRY_ROOT)]


import agent_brain_client.runtime.host_binding as host_binding
import agent_brain_client.adapters.storage.bounded_json as bounded_json


import argparse
import json
import os
import subprocess
import time

ROOT = Path(__file__).resolve().parents[4]


def verify_adapter():
    pin = ROOT / "code.json"
    if not pin.exists() and not pin.is_symlink():
        return  # Source checkout is only an installer/developer entry.
    from agent_brain_client.adapters.packages.frozen import verify_frozen_code

    verify_frozen_code(ROOT)


def probe(home, core, runner=subprocess.run, *, harness="codebuddy"):
    from agent_brain_client.runtime.host_binding import backend

    command, _ = backend(home, core)
    result = runner(
        [command["command"], *command["args"], "--harness", harness, "--installation-check"],
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
        "status": "codebuddy-backend-protocol-checked" if harness == "codebuddy" else "client-protocol-checked",
        "protocol_checked": True,
        "model_loaded": "unknown",
    }


def binding_status(home):
    from agent_brain_client.adapters.harnesses.codebuddy import configuration
    from agent_brain_client.adapters.storage import collection

    value = configuration.status(home, None, backend_check=host_binding.inspect_backend)
    if value.get("installed"):
        value["source_enabled"] = value["collection_hooks_installed"] and collection.approved(
            collection.load(home, verify_code=False)
        )
    return value


def binding_source(home, source):
    """Use the installer's prepared source or the existing verified active package."""
    from agent_brain_client.adapters.packages.client import ClientReleases
    from agent_brain_client.adapters.packages.frozen import runtime_pins
    from agent_brain_client.adapters.storage import collection, registration

    pins = collection.load(home, verify_code=False)["code_sha256"]
    if runtime_pins(source) == pins:
        host_binding.load_core(home)
        return source
    meta, _ = registration.registration(home)
    active = ClientReleases(
        meta["release_policy"], meta["release_policy_sha256"], inspection=True
    ).verified_active(serving=True)
    if active is not None:
        package = Path(active["package"])
        if runtime_pins(package, manifest_sha256=active["manifest_sha256"]) == pins:
            return package
    raise ValueError("Update the existing verified Client before binding CodeBuddy")


def configure_binding(home, *, surface=None, source=ROOT):
    from agent_brain_client.adapters.harnesses.codebuddy import configuration
    from agent_brain_client.adapters.storage import collection

    current = binding_status(home)
    if current.get("installed"):
        if surface is not None and current["surface"] != surface:
            raise ValueError("Unbind the original CodeBuddy surface before changing its scope")
        return dict(current, native_changed=False)
    source = binding_source(home, source)
    checked = probe(home, None)
    approved = collection.approved(collection.load(home, verify_code=False))
    value = configuration.install(home, source, None, surface=surface)
    value["protocol_checked"] = checked["protocol_checked"]
    value["source_enabled"] = value["automatic_recall"] and approved
    return value


def binding_review(action, error):
    return {
        "status": "codebuddy-review-or-resume-required",
        "operation": action,
        "error_type": type(error).__name__,
        "preserve_existing_state": True,
        "next_action": "Check the shared Client and CodeBuddy binding. Resume the same operation after resolving any conflicting owner or surface. Preserving the shared Client requires the CodeBuddy unbind action.",
    }


def main(argv=None, *, verified_release=False):
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "hook"))
    parser.add_argument("--device-home", type=Path, default=Path.home())
    args = parser.parse_args(argv)
    try:
        if not verified_release:
            verify_adapter()
        home = bounded_json.canonical(args.device_home)
        from agent_brain_client.adapters.platforms.filesystem import require_platform

        require_platform("codebuddy-host")
        if args.action == "serve":
            return host_binding.execute_mcp(home, host_binding.load_core(home))
        import agent_brain_client.adapters.harnesses.codebuddy.events as runtime
        if verified_release:
            from agent_brain_client.runtime.collection import native_services
            core = native_services(verify_code=False)
        else:
            core = host_binding.load_core(home)
        initialized = time.monotonic()
        from agent_brain_client.application.control import (
            RECALL_TIMING_ENV, mark_recall_timing, recall_timing_scope,
        )
        from agent_brain_client.runtime.delivery import schedule_assistant_completion
        observation = {}
        with recall_timing_scope(os.environ.get(RECALL_TIMING_ENV) == "1", started=started):
            mark_recall_timing("hook_initialized")
            output, state = runtime.hook(home, sys.stdin.buffer, core=core,
                defer=lambda turn: schedule_assistant_completion(home, turn),
                observe=observation.update, verified_release=verified_release)
            state["cli_timing_ms"] = {
                "initialization_ms": round((initialized - started) * 1000, 3),
                "total_ms": round((time.monotonic() - started) * 1000, 3),
            }
            mark_recall_timing("output_prepared")
            print(json.dumps(output, ensure_ascii=False), flush=True)
            mark_recall_timing("output_flushed")
            if observation and observation["status"] != "failed":
                from agent_brain_client.adapters.storage.recall_receipts import save_recall_observation

                observation["output_written"] = True
                try:
                    save_recall_observation({"device_home": str(home)}, observation)
                except (OSError, ValueError):
                    pass  # Flushed stdout is already observable; never emit it twice.
        # Keep diagnostics content-free. A pending queue is not a save.
        diagnostic = {
            key: state[key]
            for key in (
                "status",
                "error_type",
                "recall_status",
                "recall_error_type",
                "assistant_capture",
                "timing_ms",
                "cli_timing_ms",
                "delivery_schedule",
                "schedule_error_type",
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
    except Exception as error:
        if args.action == "hook":
            print("{}")
        print(
            json.dumps(binding_review(args.action, error)),
            file=sys.stderr,
        )
        return 0 if args.action == "hook" else 2


if __name__ == "__main__":
    sys.exit(main())
