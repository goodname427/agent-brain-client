"""Compose Client lifecycle with the exact registered native content port."""

from pathlib import Path
from agent_brain_client.adapters.installation import lifecycle as service
from agent_brain_client.adapters.installation.installation import registered_context
from agent_brain_client.adapters.storage import metadata
from agent_brain_client.runtime.harness_content import content_host
from agent_brain_client.adapters.installation.lifecycle import (
    read_removal as read_removal, journal_path as journal_path, decode as decode,
    lease as lease, queue_counts as queue_counts, state_path as state_path,
    retained_mcp_enabled as retained_mcp_enabled, retained_memory_ids as retained_memory_ids,
    remove_table as remove_table,
)


def registered_host(home, journal=None):
    file = Path(home) / ".config/agent-brain-client/codex-mcp.json"
    saved = journal if journal is not None else read_removal(home) if journal_path(home).exists() else None
    if saved is not None and saved.get("phase") in ("prepared", "applying", "complete"):
        meta = saved["registration"]
        policy = metadata.private_json(meta["release_policy"], meta["release_policy_sha256"])
        config = str(Path(policy["runtime"]["codex_home"]) / "config.toml")
        image = decode(saved["before"][config]["data"])
    elif file.exists():
        meta = metadata.private_json(file)
        image = None
    else:
        return None
    _, command, _, _ = registered_context(home, meta, image)
    args = command["args"]
    return content_host(args[args.index("--harness") + 1])


def adapter_for(home, meta, config_image=None):
    return service.adapter_for(home, meta, config_image, host=registered_host(home))


def primary_status(home):
    try:
        host = registered_host(home)
    except Exception:
        host = None
    return service.status(home, host=host)


def repair(home):
    return service.repair(home, host=registered_host(home))


def uninstall(home, confirm):
    host = registered_host(home)
    projections = secondary_projections(home, host)
    from agent_brain_client.adapters.installation import projection_removal
    # Validate every queue and owned file before the user's single confirmation.
    for session in projections:
        counts = queue_counts(session)
        if any(counts[state] for state in ("pending", "conflict", "blocked")):
            return {"status": "uninstall-unconfirmed-writes", "installed": True, "native_changed": False,
                    "harness": session.host.name, "queue": counts,
                    "next_action": "Keep the installation. Sync the original queue or resolve its conflicts through this Harness, then rerun uninstall."}
        projection_removal.prepare(session)
    def finish_hosts(message):
        answer = confirm(message)
        if answer == "Continue":
            for session in projections:
                projection_removal.remove(session)
            remove_secondary_entries(home)
        return answer
    result = service.uninstall(home, finish_hosts, host=host)
    if result.get("status") == "client-uninstalled":
        remove_secondary_entries(home)
    return result


def prepare_removal(home):
    return service.prepare_removal(home, host=registered_host(home))


def validate_journal(home, journal):
    return service.validate_journal(home, journal, host=registered_host(home, journal))


def validate_uninstalled_state(home, journal, **options):
    return service.validate_uninstalled_state(home, journal, host=registered_host(home, journal), **options)


def finish_removal(home, journal):
    return service.finish_removal(home, journal, host=registered_host(home, journal))


def secondary_projections(home, primary):
    """Construct metadata views only for separately registered content roots."""
    from agent_brain_client.runtime.host_maintenance import registered_hosts
    from agent_brain_client.adapters.storage.registration import registration
    from agent_brain_client.adapters.storage.content_session import ContentSession
    from agent_brain_client.adapters.platforms.filesystem import native_platform
    if primary is None:
        return []
    names = [name for name in registered_hosts(home) if name != primary.name]
    if not names:
        return []
    file = Path(home) / ".config/agent-brain-client/codex-mcp.json"
    if file.exists():
        _, policy = registration(home)
    else:
        saved = read_removal(home)["registration"]
        policy = metadata.private_json(saved["release_policy"], saved["release_policy_sha256"])
    content = metadata.private_json(policy["runtime"]["content_policy"], policy["runtime"]["content_policy_sha256"])
    result = []
    for name in names:
        port = content_host(name)
        native, cache = port.location(home, policy["runtime"])
        if not Path(cache).exists():
            continue
        result.append(ContentSession.metadata_view(home, native, cache, content, platform=native_platform(), host=port))
    return result


def remove_secondary_entries(home):
    from agent_brain_client.runtime.host_maintenance import registered_hosts
    if "codex" in registered_hosts(home):
        from agent_brain_client.adapters.harnesses.codex import binding
        file = Path(home) / ".config/agent-brain-client/codex-mcp.json"
        if file.exists():
            binding.install(home, remove=True)
    if "codebuddy" in registered_hosts(home):
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        file = configuration.state_directory(home) / "registration.json"
        receipt = metadata.private_json(file)
        configuration.install(Path(home), Path(__file__).resolve().parents[3], None,
                              surface=receipt["surface"], remove=True)


def status(home):
    result = primary_status(home)
    from agent_brain_client.runtime.collection import host_status
    from agent_brain_client.adapters.storage.collection import settings_path
    settings = settings_path(home)
    value = metadata.private_json(settings) if settings.exists() else {"device_home": str(home)}
    result["hosts"] = host_status(value)
    try:
        host = registered_host(home)
        result["primary_harness"] = host.name if host is not None else None
        result["host_installations"] = {
            name: {"installed": row.get("mcp_installed", row.get("installed")),
                   "collection_entry_installed": row.get("installed"),
                   "status": row.get("status"), "service_enabled": row.get("service_enabled")}
            for name, row in result["hosts"].items()}
        if host is not None:
            row = result["host_installations"].setdefault(host.name, {})
            row["registered_client_verified"] = result.get("registration_verified", result.get("uninstall_verified", False))
            if host.name == "codex" and "codex" not in result["hosts"]:
                row["installed"] = result.get("registration_verified", False)
                row["service_enabled"] = result.get("installed", False)
        result["host_content"] = {}
        for session in secondary_projections(home, host) if (Path(home) / ".config/agent-brain-client/codex-mcp.json").exists() else []:
            result["host_content"][session.host.name] = {
                "queue": queue_counts(session), "owned_mapping_files": len(session.mapping.owned_snapshot()["files"])}
    except Exception:
        result["host_content_status"] = "review-required"
    return result
