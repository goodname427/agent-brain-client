"""Install stages and explicit local future-source setup on Mac and Windows.

No directory scan, native history read, credential creation or Server authority.
The existing device joins its existing service automatically for future project
sessions. Mapping, collection and host loading report separate results.
"""

import json
import os
from pathlib import Path
import subprocess
from typing import Any
from types import SimpleNamespace


def resume_setup(home, native):
    """Dispatch owned pending transactions before original-install inspection."""
    import content_install as ci
    from global_memory_install import recover_pending
    from first_install import manage_update

    stage = "hook-recovery"
    try:
        recover_pending(home, native)
        marker = ci.safe(
            Path(home) / ".config/agent-brain-client/launcher-handover.json"
        )
        if marker.exists():
            stage = "launcher-recovery"
            saved = ci.private_json(marker)
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
    from client_release import ClientReleases, LEGACY_PRESERVATION, DEPENDENCY_RUNTIME
    import content_install as ci

    metadata = ci.private_json(Path(home) / ".config/agent-brain-client/codex-mcp.json")
    active = ClientReleases(
        metadata["release_policy"], metadata["release_policy_sha256"]
    ).verified_active()
    if active is None:
        raise ValueError("Verified active runtime required")
    command = [
        mcp["command"],
        "-I",
        "-B",
        str(Path(active["package"]) / "adapters/codex/content_mcp.py"),
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
    """Installation and updates enable future collection for every project."""
    from global_memory_operator import configure
    result = configure(SimpleNamespace(device_home=str(home), include_root=[], isolate_root=[],
        exclude_root=[], non_project_root=[], all_projects=True, activate_packet=None,
        approve_scope=None, packet_output=None))
    return dict(result, collection_enabled=result.get('activated') is True)


def complete(home, result, *, choose=input, refresh=mapping_refresh):
    """Continue an authorized install; each stage reports independent evidence."""
    import content_install as ci
    import global_memory_install as hooks

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
        meta = ci.private_json(Path(home) / ".config/agent-brain-client/codex-mcp.json")
        policy, mcp, _, _ = ci.registered_context(home, meta)
        if not policy["platform"].startswith(("win32-", "darwin-")):
            return result
        if mcp.get("enabled") is False:
            stages.update(
                mapping={"status": "paused"},
                hook={"status": "paused"},
                sources={"status": "paused", "collection_enabled": False},
            )
            return value
        stage = "mapping"
        stages["mapping"] = refresh(home, policy, mcp)
        stage = "hook"
        stages["hook"] = hooks.install(
            SimpleNamespace(
                device_home=str(home),
                profile_id=None,
                operator=None,
                operator_sha256=None,
            )
        )
        stages["hook_trust"] = "normal-codex-review-required"
        stage = "sources"
        stages["sources"] = source_setup(home, choose=choose)
        value["local_setup_complete"] = (
            stages["mapping"].get("status") in ("content-current", "content-refreshed")
            and stages["sources"].get("collection_enabled") is True
        )
        value["next_action"] = (
            "Review installation_stages. Observe fresh MCP client_runtime version and commit before deciding whether only Agent Brain MCP needs a reload. If unobservable, report unknown. Review inline Hook trust in normal Codex settings when pending; use a fresh conversation when mapped Rules/Skills need loading. Follow the sources next_action if source selection or Server admission is pending."
        )
    except Exception:
        stages["setup"] = {"status": "pending-review-or-resume", "stage": stage}
        value["next_action"] = (
            "Rerun the normal Windows install entry to resume the incomplete stage. Preserve configuration and recovery receipts; MCP registration alone does not confirm mapping or global memory activation."
        )
    return value
