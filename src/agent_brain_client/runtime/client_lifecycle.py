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
    host = registered_host(home)
    return service.repair(home, host=host, remove_hosts=lambda names: _remove_hosts(home, host, names))


def uninstall(home, confirm):
    host = registered_host(home)
    return service.uninstall(
        home, confirm, host=host,
        prepare_hosts=lambda: _prepare_hosts(home, host),
        remove_hosts=lambda names: _remove_hosts(home, host, names),
    )


def _prepare_hosts(home, primary):
    from agent_brain_client.adapters.installation import projection_removal
    from agent_brain_client.adapters.storage.registration import codex_registration

    # An unbound host can still have a queue and an owned projection.
    names = []
    if codex_registration(home) is not None:
        names.append("codex")
    buddy = state_path(home) / "codebuddy/registration.json"
    if buddy.exists() or buddy.is_symlink():
        metadata.private_json(buddy)
        names.append("codebuddy")
    for session in secondary_projections(home, primary, names=names):
        counts = queue_counts(session)
        if any(counts[state] for state in ("pending", "conflict", "blocked")):
            return {"status": "uninstall-unconfirmed-writes", "installed": True, "native_changed": False,
                    "harness": session.host.name, "queue": counts,
                    "next_action": "Keep the installation. Sync the original queue or resolve its conflicts through this Harness, then rerun uninstall."}
        # An older device uninstall could leave only the projection receipt.
        # Its own recovery restores the preimages before preparing them again.
        if not projection_removal.removal_in_progress(session):
            projection_removal.prepare(session)
    return {"pending_harnesses": names} if names else {}


def _remove_hosts(home, primary, names):
    from agent_brain_client.adapters.installation import projection_removal

    for name in names:
        content_host(name)
    for session in secondary_projections(home, primary, names=names):
        projection_removal.remove(session)
    for name in names:
        _detach_native_binding(home, name)


def unbind(home, *, harness):
    """Detach one native entry while retaining its projection and the Client."""
    content_host(harness)
    result = _detach_native_binding(home, harness)
    if result["status"] == "retained-direct-codex-binding":
        return {"status": "host-unbind-shared-layout", "harness": harness,
                "native_changed": False,
                "next_action": "Use the documented structural reinstall to move Client control into its private directory before unbinding Codex."}
    return dict(result, status="host-unbound", harness=harness)


def _detach_native_binding(home, harness):
    if harness == "codex":
        from agent_brain_client.adapters.harnesses.codex import binding

        receipt = binding.receipt(home)
        shared = state_path(home) / "codex-mcp.json"
        if not (shared.exists() or shared.is_symlink()) and (receipt is None or receipt["removed"]):
            return {"status": "codex-uninstalled", "native_changed": False}
        meta = metadata.private_json(shared)
        policy = metadata.private_json(meta["release_policy"], meta["release_policy_sha256"])
        if binding.private_control(home, policy["runtime"]):
            if receipt is None:
                return {"status": "codex-uninstalled", "native_changed": False}
            from agent_brain_client.adapters.harnesses.codex import collection_installation as hooks
            hooks.recover_pending(home, Path(receipt["native_home"]))
        return binding.install(home, remove=True)

    from agent_brain_client.adapters.harnesses.codebuddy import configuration

    file = configuration.state_directory(home) / "registration.json"
    if not file.exists():
        return {"status": "codebuddy-not-installed", "native_changed": False}
    receipt = metadata.private_json(file)
    return configuration.install(Path(home), Path(__file__).resolve().parents[3], None,
                                 surface=receipt["surface"], remove=True)


def prepare_removal(home):
    return service.prepare_removal(home, host=registered_host(home))


def validate_journal(home, journal):
    return service.validate_journal(home, journal, host=registered_host(home, journal))


def validate_uninstalled_state(home, journal, **options):
    return service.validate_uninstalled_state(home, journal, host=registered_host(home, journal), **options)


def finish_removal(home, journal):
    host = registered_host(home, journal)
    return service.finish_removal(home, journal, host=host,
                                  remove_hosts=lambda names: _remove_hosts(home, host, names))


def secondary_projections(home, primary, *, names=None):
    """Construct metadata views only for separately registered content roots."""
    from agent_brain_client.runtime.host_maintenance import registered_hosts
    from agent_brain_client.adapters.storage.registration import registration
    from agent_brain_client.adapters.storage.content_session import ContentSession
    from agent_brain_client.adapters.platforms.filesystem import native_platform
    if primary is None:
        return []
    names = [name for name in (registered_hosts(home) if names is None else names) if name != primary.name]
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


def status(home):
    result = primary_status(home)
    from agent_brain_client.runtime.delivery import runtime_status
    from agent_brain_client.runtime.collection import host_status
    from agent_brain_client.adapters.storage.collection import settings_path

    if result.get("installed_commit"):
        result.update(runtime_status(home, {
            "version": result["installed_version"], "commit": result["installed_commit"],
        }))
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
