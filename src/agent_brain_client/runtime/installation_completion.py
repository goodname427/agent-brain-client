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


def source_setup(home, *, choose=input, initialize=False):
    """Reconcile a source; only installation completion may select its default."""
    from agent_brain_client.runtime.source_enrollment import maintain
    from agent_brain_client.adapters.storage.reinstallation import (
        read_journal,
        require_memory_verified,
    )

    require_memory_verified(home)
    return maintain(home, initialize=initialize, retained=read_journal(home))


def finish_windows_update(home, result):
    """Retry the missing retained native binding after a confirmed Windows update."""
    if os.name != "nt" or result.get("status") not in ("client-update-updated", "client-update-current"):
        return result
    from agent_brain_client.adapters.storage.reinstallation import read_journal

    fresh = read_journal(home)
    if (
        fresh is None or fresh["phase"] != "complete" or fresh.get("native_bindings_restored")
        or "codex" not in fresh["mapping"].get("registered_surfaces", {})
    ):
        return result
    return complete(
        home,
        dict(result, installed=True, retained_archive=fresh["archive"],
             old_identity_reused=False, old_queue_replayed=False),
        bindings_only=True,
    )


def complete(
    home, result, *, harness=None, choose=input, refresh=mapping_refresh, bindings_only=False,
):
    """Continue an authorized install; each stage reports independent evidence."""
    import agent_brain_client.adapters.installation.installation as ci
    from agent_brain_client.adapters.storage.registration import CodexBindingStateBlocked

    value = dict(result)
    if result.get("installed") is not True:
        return value
    stages: dict[str, Any] = {
        "mcp_registration": "unverified",
        "worker": "unobserved",
        "model_context": "reload-unobserved",
    }
    value["installation_stages"] = stages
    value["installation_complete"] = False
    value["local_setup_complete"] = False
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
        stages["mcp_registration"] = "verified"
        selected = harness if harness is not None else mcp["args"][mcp["args"].index("--harness") + 1]
        if selected not in ("codex", "codebuddy"):
            raise ValueError("Explicit installation Harness required")
        if not policy["platform"].startswith(("win32-", "darwin-")):
            return result
        from agent_brain_client.runtime.fresh_installation import restore_native_bindings

        stage = "retained-native-bindings"
        if selected == "codex":
            stages["native_mcp"] = {"status": "pending"}
        try:
            native_mcp = restore_native_bindings(home, keep_configured=bindings_only)
        except CodexBindingStateBlocked:
            stage = "native-mcp"
            raise
        if native_mcp is not None:
            stages["native_mcp"] = native_mcp
        if bindings_only:
            if native_mcp is not None and native_mcp["native_changed"]:
                value["native_changed"] = True
                value["reload_check"] = dict(
                    value.get("reload_check", {}), mcp_registration_changed=True,
                )
            return value
        if native_mcp is None and selected == "codex":
            stage = "native-mcp"
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
        stages["sources"] = source_setup(home, choose=choose, initialize=True)
        value["local_setup_complete"] = (
            stages["mapping"].get("status") in ("content-current", "content-refreshed")
            and stages["sources"].get("collection_enabled") is True
        )
        value["next_action"] = (
            "Review installation_stages. Observe fresh MCP client_runtime version and commit before deciding whether only Agent Brain MCP needs a reload. If unobservable, report unknown. Review Hook trust in the selected host settings when pending; use a fresh conversation when mapped Rules/Skills need loading. Follow the sources next_action if source selection or Server admission is pending."
        )
    except Exception as error:
        stages["setup"] = {"status": "pending-review-or-resume", "stage": stage}
        if stage == "native-mcp":
            stages["native_mcp"] = {"status": "blocked"}
            if isinstance(error, CodexBindingStateBlocked):
                stages["native_mcp"]["reason"] = "private-codex-binding-state-unverified"
        entry = "update" if bindings_only else "install"
        value["next_action"] = (
            f"Rerun the normal Windows {entry} entry to resume the incomplete stage. "
            "Preserve configuration and recovery receipts."
        )
    return value
