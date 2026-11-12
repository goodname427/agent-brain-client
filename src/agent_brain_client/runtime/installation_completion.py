"""Install stages and explicit local future-source setup on Mac and Windows.

No directory scan, native history read, credential creation or Server authority.
The existing device joins its existing service automatically for future project
sessions. Mapping, collection and host loading report separate results.
"""

import agent_brain_client.adapters.storage.private_files as private_files


import json
import os
from pathlib import Path
import subprocess
from typing import Any
from types import SimpleNamespace


def resume_setup(home, native):
    """Dispatch owned pending transactions before original-install inspection."""
    import agent_brain_client.adapters.installation.installation as ci
    from agent_brain_client.runtime.host_maintenance import recover_pending
    from agent_brain_client.runtime.updates import manage_update

    stage = "hook-recovery"
    try:
        recover_pending(home, native)
        marker = private_files.safe(
            Path(home) / ".config/agent-brain-client/launcher-handover.json"
        )
        if marker.exists():
            stage = "launcher-recovery"
            saved = private_files.private_json(marker)
            _, bound_native, _ = ci.validate_install_scope(saved["plan"])
            if str(bound_native) != str(native):
                raise ValueError("Original native binding required")
            result = manage_update(home, "update")
            if result.get("status") not in (
                "client-update-current",
                "client-update-updated",
            ):
                return dict(
                    result,
                    installed=False,
                    installation_complete=False,
                    recovery_stage=stage,
                )
        return None
    except Exception:
        return {
            "status": "windows-setup-recovery-review-required",
            "installed": False,
            "native_changed": None,
            "installation_complete": False,
            "recovery_stage": stage,
            "next_action": "Preserve the original policy, registration and setup journals. Resume the normal Windows install entry after reviewing the named recovery stage; do not reset data or edit digests.",
        }


def mapping_refresh(home, policy, mcp, *, runner=subprocess.run):
    from agent_brain_client.adapters.packages.client import (
        ClientReleases,
        LEGACY_PRESERVATION,
        DEPENDENCY_RUNTIME,
    )

    metadata = private_files.private_json(
        Path(home) / ".config/agent-brain-client/codex-mcp.json"
    )
    active = ClientReleases(
        metadata["release_policy"], metadata["release_policy_sha256"]
    ).verified_active()
    if active is None:
        raise ValueError("Verified active runtime required")
    command = [
        mcp["command"],
        "-I",
        "-B",
        str(
            Path(active["package"]) / "src/agent_brain_client/interfaces/mcp/content.py"
        ),
        "--harness",
        mcp["args"][mcp["args"].index("--harness") + 1],
        "--client-version",
        active["version"],
        "--mapping-refresh",
    ]
    for name, value in policy["runtime"].items():
        if name not in LEGACY_PRESERVATION | DEPENDENCY_RUNTIME:
            command.extend(
                ["--" + name.replace("_", "-").replace("content-cache", "cache"), value]
            )
    environment = dict(
        os.environ,
        **mcp.get("env", {}),
        HOME=str(home),
        USERPROFILE=str(home),
        CODEX_HOME=policy["runtime"]["codex_home"],
    )
    run = runner(
        command,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=150,
        env=environment,
    )
    if len(run.stdout) > 16384:
        raise ValueError("Bounded mapping result required")
    value = json.loads(run.stdout)
    if run.returncode or value.get("status") not in (
        "content-refreshed",
        "content-current",
    ):
        return {
            "status": "mapping-pending",
            "result": value,
            "model_loaded": "unknown",
            "next_action": "Rerun the normal install entry to resume mapping; preserve the content transaction and user files.",
        }
    return value


def source_setup(home, *, choose=input):
    """Set up future collection, preserving an explicitly retained scope/pause."""
    from agent_brain_client.runtime.source_enrollment import configure
    from agent_brain_client.adapters.storage.reinstallation import read_journal
    from agent_brain_client.adapters.storage.reinstallation import (
        require_memory_verified,
    )
    from agent_brain_client.adapters.storage.collection import load, settings_path

    require_memory_verified(home)
    fresh = read_journal(home)
    retain_scope = fresh is not None and fresh.get("routing") is not None
    if retain_scope:
        value = load(home, verify_code=False)
        if not value["enabled"] or (settings_path(home).parent / "global.off").exists():
            return {
                "status": "retained-source-paused",
                "collection_enabled": False,
                "next_action": "The original source remains paused. Use explicit global configuration/resume when you choose to enable it.",
            }

    result = configure(
        SimpleNamespace(
            device_home=str(home),
            include_root=[],
            isolate_root=[],
            exclude_root=[],
            non_project_root=[],
            all_projects=not retain_scope,
            preserve_scope=retain_scope,
            activate_packet=None,
            packet_output=None,
        )
    )
    return dict(result, collection_enabled=result.get("activated") is True)


def complete(home, result, *, harness=None, choose=input, refresh=mapping_refresh):
    """Continue an authorized install; each stage reports independent evidence."""
    import agent_brain_client.adapters.installation.installation as ci

    value = dict(result)
    if result.get("installed") is not True:
        return value
    stages: dict[str, Any] = {
        "mcp_registration": "verified",
        "worker": "unobserved",
        "model_context": "reload-unobserved",
    }
    value["installation_stages"] = stages
    value["installation_complete"] = False
    stage = "registration"
    try:
        from agent_brain_client.adapters.storage.reinstallation import (
            require_memory_verified,
        )

        require_memory_verified(home)
        meta = private_files.private_json(
            Path(home) / ".config/agent-brain-client/codex-mcp.json"
        )
        policy, mcp, _, _ = ci.registered_context(home, meta)
        selected = harness if harness is not None else mcp["args"][mcp["args"].index("--harness") + 1]
        if selected not in ("codex", "codebuddy"):
            raise ValueError("Explicit installation Harness required")
        if not policy["platform"].startswith(("win32-", "darwin-")):
            return result
        from agent_brain_client.runtime.fresh_installation import restore_native_bindings
        restore_native_bindings(home)
        if selected == "codex":
            from agent_brain_client.adapters.harnesses.codex import binding
            previous = binding.receipt(home)
            stages["native_mcp"] = (binding.install(home, native=Path(previous["native_home"]), enabled=previous["mcp"]["enabled"])
                                    if previous is not None and previous["removed"] else binding.install(home))
        if mcp.get("enabled") is False:
            stages.update(
                mapping={"status": "paused"},
                hook={"status": "paused"},
                sources={"status": "paused", "collection_enabled": False},
            )
            return value
        stage = "mapping"
        import copy
        selected_mcp = copy.deepcopy(mcp)
        selected_mcp["args"][selected_mcp["args"].index("--harness") + 1] = selected
        stages["mapping"] = refresh(home, policy, selected_mcp)
        stage = "hook"
        from agent_brain_client.runtime.host_maintenance import install_sources
        stages["hook"] = install_sources(
            SimpleNamespace(
                device_home=str(home),
                profile_id=None,
                operator=None,
                operator_sha256=None,
                harness=selected,
            )
        )
        stages["hook_trust"] = "normal-host-review-required"
        stage = "source-routing"
        from agent_brain_client.runtime.fresh_installation import restore_source_routing

        restore_source_routing(home)
        stage = "sources"
        stages["sources"] = source_setup(home, choose=choose)
        value["local_setup_complete"] = (
            stages["mapping"].get("status") in ("content-current", "content-refreshed")
            and stages["sources"].get("collection_enabled") is True
        )
        value["next_action"] = (
            "Review installation_stages. Observe fresh MCP client_runtime version and commit before deciding whether only Agent Brain MCP needs a reload. If unobservable, report unknown. Review Hook trust in the selected host settings when pending; use a fresh conversation when mapped Rules/Skills need loading. Follow the sources next_action if source selection or Server admission is pending."
        )
    except Exception:
        stages["setup"] = {"status": "pending-review-or-resume", "stage": stage}
        value["next_action"] = (
            "Rerun the normal Windows install entry to resume the incomplete stage. Preserve configuration and recovery receipts; MCP registration alone does not confirm mapping or global memory activation."
        )
    return value
