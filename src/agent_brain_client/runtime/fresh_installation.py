"""Compose fresh-install ports; installed data stays in its explicit archive."""

from pathlib import Path
from agent_brain_client.adapters.storage.reinstallation import RetainedInstallation
from agent_brain_client.adapters.installation.fresh_installation import (
    detach_payload,
    retained_mapping,
    retained_registration,
    restore_mapping,
)


def host_payload(home, native, metadata, *, installed=True):
    host = projection_host(home, native)
    payload = detach_payload(home, native, metadata, installed=installed, host=host)
    from agent_brain_client.runtime.host_maintenance import registered_hosts
    if host.name != "codebuddy" and "codebuddy" in registered_hosts(home):
        from agent_brain_client.adapters.harnesses.codebuddy.configuration import fresh_installation_payload
        payload.update(fresh_installation_payload(home))
    return payload


def retained_installation(home, native, *, target_host=None):
    def location(cache):
        host = projection_host(home, native)
        return (Path(native), Path(cache)) if host.name == "codex" else (Path(home) / ".codebuddy", Path(cache))
    def read_mapping(home, native, cache, content):
        port = projection_host(home, native)
        native, cache = location(cache)
        snapshot = retained_mapping(home, native, cache, content, host=port)
        from agent_brain_client.runtime.client_lifecycle import secondary_projections
        extra = secondary_projections(home, port)
        if extra:
            snapshot["additional_projections"] = {
                session.host.name: {"native": str(session.native_home), "mapping": session.mapping.retention_snapshot()}
                for session in extra}
        from agent_brain_client.runtime.host_maintenance import registered_hosts
        if "codebuddy" in registered_hosts(home):
            from agent_brain_client.adapters.storage.metadata import private_json
            receipt = private_json(Path(home) / ".config/agent-brain-client/codebuddy/registration.json")
            snapshot["registered_surfaces"] = {"codebuddy": {"surface": receipt["surface"],
                "hooks_enabled": receipt.get("hooks_enabled", True)}}
        return snapshot
    def restore(home, native, cache, content, snapshot):
        original = projection_host(home, native)
        selected = target_host if target_host is not None else original
        original_native, _ = location(cache)
        primary_cache = Path(cache) if selected.name == original.name else Path(home) / ".config/agent-brain-client/retained-projections" / original.name
        restore_mapping(home, original_native, primary_cache, content, snapshot, host=original)
        from agent_brain_client.runtime.harness_content import content_host
        for name, row in snapshot.get("additional_projections", {}).items():
            port = content_host(name)
            target_cache = (Path(cache) if name == selected.name else
                            Path(home) / ".config/agent-brain-client/codebuddy/content-cache" if name == "codebuddy" else
                            Path(home) / ".config/agent-brain-client/retained-projections" / name)
            expected = port.binding(home, Path(row["native"]))
            if row["mapping"]["owned"]["binding"] != expected:
                raise ValueError("Retained secondary projection identity changed")
            restore_mapping(home, Path(row["native"]), target_cache, content, row["mapping"], host=port)
    return RetainedInstallation(
        Path(home),
        Path(native),
        host_payload=host_payload,
        read_mapping=read_mapping,
        restore_mapping=restore,
        read_registration=retained_registration,
        read_project_controls=project_controls,
    )


def project_controls(home):
    from agent_brain_client.adapters.storage.collection import project_references
    from agent_brain_client.adapters.storage.project_settings import (
        retained_controls,
    )

    from agent_brain_client.runtime.host_maintenance import registered_hosts
    return retained_controls(home, project_references(home), isolate_hosts="codebuddy" in registered_hosts(home))


def verify_memory(home):
    """Read known retained Memory IDs through the newly authenticated device."""
    from agent_brain_client.adapters.storage.registration import registration
    from agent_brain_client.adapters.server.https import HTTPSContentClient
    from agent_brain_client.adapters.storage.reinstallation import read_journal
    from agent_brain_client.application.installation.fresh import (
        finish_fresh_installation,
    )
    from agent_brain_client.adapters.installation.fresh_installation import (
        retained_memory_ids,
    )

    record = read_journal(home)
    if not record or record["phase"] not in ("ready", "complete"):
        return None
    _, policy = registration(home)
    runtime = policy["runtime"]
    client = HTTPSContentClient(
        runtime["https_profile"], runtime["https_profile_sha256"]
    )
    identities = sorted(
        set(retained_memory_ids(record["mapping"])) | set(record.get("memory_ids", []))
    )
    return finish_fresh_installation(
        retained_installation(home, Path(record["native"])),
        client,
        runtime["https_profile_sha256"],
        identities,
    )


def restore_source_routing(home):
    from agent_brain_client.adapters.storage.reinstallation import read_journal
    from agent_brain_client.adapters.storage.collection import (
        seed_retained_routing,
        seed_project_stops,
    )

    record = read_journal(home)
    if (
        record is None
        or record["phase"] != "complete"
        or record.get("routing_restored")
    ):
        return
    if record.get("routing") is not None:
        seed_retained_routing(home, record["routing"])
    seed_project_stops(home, record["project_controls"]["blocked_roots"])
    from agent_brain_client.adapters.storage.project_settings import seed_host_stops
    seed_host_stops(home, record["project_controls"]["states"], include_legacy=record["project_controls"].get("host_scoped") is True)
    record["routing_restored"] = True
    retained_installation(home, Path(record["native"])).save(record)


def projection_host(home, native):
    """Read the exact retained projection schema without loading its old code."""
    from agent_brain_client.adapters.storage import metadata
    from agent_brain_client.adapters.storage.reinstallation import read_journal
    from agent_brain_client.runtime.harness_content import content_host
    root = Path(home) / ".config/agent-brain-client"
    record = read_journal(home)
    if record is not None:
        binding = record["mapping"]["owned"]["binding"]
    else:
        if (root / "codex-mcp.json").exists():
            meta = metadata.private_json(root / "codex-mcp.json")
        else:
            from agent_brain_client.adapters.installation.lifecycle import read_removal
            meta = read_removal(home)["registration"]
        policy = metadata.private_json(meta["release_policy"], meta["release_policy_sha256"])
        cache = Path(policy["runtime"]["content_cache"])
        file = cache / "owned.json"
        if file.exists():
            binding = metadata.private_json(file, cache=True)["binding"]
        else:
            from agent_brain_client.adapters.installation.installation import registered_context
            _, command, _, _ = registered_context(home, meta)
            args = command["args"]
            if "--harness" in args:
                return content_host(args[args.index("--harness") + 1])
            # An authenticated pre-Harness launcher had exactly the Codex
            # projection contract. Read its retained data; never run it as a
            # current default backend or add a forwarding implementation.
            return content_host("codex")
    if set(binding) == {"device_home", "codex_home"}:
        return content_host("codex")
    if set(binding) == {"device_home", "host", "native_home"} and binding["host"] == "codebuddy":
        return content_host("codebuddy")
    raise ValueError("Unknown retained projection identity")


def restore_native_bindings(home):
    """Rebind only the exact previously registered surface and Hook pause state."""
    from agent_brain_client.adapters.storage.reinstallation import read_journal
    record = read_journal(home)
    if record is None or record["phase"] != "complete" or record.get("native_bindings_restored"):
        return
    surfaces = record["mapping"].get("registered_surfaces", {})
    if set(surfaces) - {"codebuddy"}:
        raise ValueError("Unknown retained native surface")
    if "codebuddy" in surfaces:
        row = surfaces["codebuddy"]
        if set(row) != {"surface", "hooks_enabled"} or row["surface"] not in ("both", "cli", "ide") or type(row["hooks_enabled"]) is not bool:
            raise ValueError("Exact retained CodeBuddy surface required")
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        configuration.install(Path(home), Path(__file__).resolve().parents[3], None,
                              surface=row["surface"], enable_hooks=row["hooks_enabled"])
    record["native_bindings_restored"] = True
    retained_installation(home, Path(record["native"])).save(record)
