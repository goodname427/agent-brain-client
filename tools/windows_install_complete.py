"""Install stages and explicit local future-source setup on Mac and Windows.

No directory scan, native history read, credential creation or Server authority.
A metadata packet is prepared only after the person chooses roots and approves
that exact scope in this local flow. Server admission and host trust stay separate.
"""

import copy
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
    from mac_first_install import manage_update

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
    import global_memory as gm
    from global_memory_operator import configure
    from global_memory_scope import validate, digest

    value = gm.load(home)
    directory = gm.settings_path(home).parent
    if gm.approved(value):
        return {
            "status": "source-active",
            "collection_enabled": True,
            "scope_sha256": digest(value["scope"]),
        }
    if value["approved_scope_sha256"] or (directory / "global.off").exists():
        packet = directory / "device-source-packet.json"
        return {
            "status": "source-paused-or-admission-pending",
            "collection_enabled": False,
            "packet": str(packet) if packet.exists() else None,
            "next_action": "Keep the existing scope and pause. Retry pending registration with global-configure --activate-packet and the saved packet; resume an intentionally paused source only by explicit global-resume.",
        }
    print(
        "Agent Brain global memory: Hook installation does not approve collection.\n"
        "Choose local roots explicitly. Only future Hook events in the chosen scope are eligible; no history will be scanned.\n"
        "Personal roots share personal memory across projects; exclusions remain excluded.\n"
        "Choose sources / Later"
    )
    try:
        if choose("> ").strip() != "Choose sources":
            raise EOFError()
        scope = copy.deepcopy(value["scope"])
        # An empty local install starts with a random placeholder pool. Bind the
        # preview to the authenticated existing pool, not that placeholder.
        # Never overwrite an already registered server source from this flow.
        if not (scope["include_roots"] or scope["exclude_roots"] or scope["non_project_roots"] or value["legacy_projects"] or value["server_scope_sha256"]):
            import re
            client = gm.GlobalClient(value)
            response = client.wire("memory_global_status", {})
            admission = response.get("result", {})
            pool = admission.get("global_profile_id")
            if ("error" in response or admission.get("metadata_only") is not True
                    or admission.get("source_device_id") != client.device_id
                    or admission.get("source_registered") is not False
                    or not isinstance(pool, str) or not re.fullmatch(r"[a-f0-9]{32}", pool)):
                return {
                    "status": "source-server-status-review-required",
                    "collection_enabled": False,
                    "server_error": response.get("error"),
                    "next_action": "Check global-status --check-server. Preserve existing device and source configuration; do not replace a registered Server source or guess the personal pool.",
                }
            scope["profile_id"] = pool
        for key, prompt in (
            ("include_roots", "Personal project root"),
            ("exclude_roots", "Excluded root"),
            ("non_project_roots", "Personal non-project root"),
        ):
            rows = []
            for _ in range(128):
                root = choose(prompt + " (absolute path; blank to finish): ").strip()
                if not root:
                    break
                path = Path(root).expanduser()
                if (
                    not path.is_absolute()
                    or any(p.is_symlink() for p in (path, *path.parents))
                    or not path.is_dir()
                ):
                    raise ValueError("Choose an existing canonical local directory")
                root = str(path.resolve())
                rows.append(
                    {"root": root, "sharing": "personal"}
                    if key == "include_roots"
                    else root
                )
            scope[key] = rows
        validate(scope)
        if not (scope["include_roots"] or scope["non_project_roots"]):
            raise ValueError("Choose at least one source")
        print(json.dumps(scope, ensure_ascii=False, indent=2))
        print(
            "Recipient: the existing Agent Brain Server and its configured memory processor.\n"
            "This approves the displayed future sources on this device; no past messages are imported.\nApprove / Later"
        )
        if choose("> ").strip() != "Approve":
            raise EOFError()
    except (EOFError, KeyboardInterrupt):
        return {
            "status": "source-selection-required",
            "collection_enabled": False,
            "next_action": "Rerun the normal install entry and choose sources locally. Select project roots, exclusions and non-project roots, then approve the displayed scope. Until then the Hook returns before reading its input.",
        }
    if scope["profile_id"] != value["scope"]["profile_id"]:
        if gm.load(home) != value:
            raise ValueError("Global settings changed during source review")
        value["scope"]["profile_id"] = scope["profile_id"]
        gm.old.write(gm.settings_path(home), value)
    packet = directory / "device-source-packet.json"
    args = SimpleNamespace(
        device_home=str(home),
        include_root=[r["root"] for r in scope["include_roots"]],
        isolate_root=[],
        exclude_root=scope["exclude_roots"],
        non_project_root=scope["non_project_roots"],
        approve_scope=digest(scope),
        activate_packet=None,
        packet_output=str(packet),
    )
    result = configure(args)
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
