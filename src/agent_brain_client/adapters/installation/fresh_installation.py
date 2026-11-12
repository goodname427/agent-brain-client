"""Detach explicitly supplied native entries while retaining every memory projection."""

from pathlib import Path
from agent_brain_client.adapters.storage import metadata as io
from agent_brain_client.adapters.installation import registry_format as config
from agent_brain_client.adapters.installation.installation import (
    registered_mcp_digest,
)

from agent_brain_client.adapters.storage.content_session import ContentSession


def mapping_view(home, native, cache, content, *, host):
    from agent_brain_client.adapters.platforms.filesystem import native_platform

    return ContentSession.metadata_view(
        home, native, cache, content, platform=native_platform(), host=host
    ).mapping


def retained_mapping(home, native, cache, content, *, host):
    mapping = mapping_view(home, native, cache, content, host=host)
    if not mapping.transaction_settled():
        raise ValueError("Resolve pending native mapping before fresh installation")
    return mapping.retention_snapshot()


def restore_mapping(home, native, cache, content, snapshot, *, host):
    mapping_view(home, native, cache, content, host=host).restore_retention(snapshot)


def detach_payload(home, native, metadata, *, installed=True, host):
    return host.detach_payload(home, native, metadata, installed=installed)


def retained_memory_ids(snapshot):
    identities = {Path(name).stem for name in snapshot["owned"]["files"]
                  if "/memory/" in name and name.startswith(".config/agent-brain-client/content/")}
    for row in snapshot.get("additional_projections", {}).values():
        identities.update(retained_memory_ids(row["mapping"]))
    return sorted(identities)


def retained_registration(home):
    root = Path(home) / ".config/agent-brain-client"
    file = root / "codex-mcp.json"
    if file.exists():
        metadata = io.private_json(file)
        policy = io.private_json(
            Path(metadata["release_policy"]), metadata["release_policy_sha256"]
        )
        native = Path(policy["runtime"]["codex_home"])
        mcp = (
            config.tomllib.loads((native / "config.toml").read_text())
            .get("mcp_servers", {})
            .get(config.NAME)
        )
        if (
            not isinstance(mcp, dict)
            or registered_mcp_digest(mcp) != metadata["mcp_sha256"]
            or type(mcp.get("enabled")) is not bool
        ):
            raise ValueError("Original MCP pause state changed")
        return {
            "metadata": metadata,
            "installed": True,
            "service_enabled": mcp["enabled"],
            "memory_ids": [],
            "source": {str(file): io.digest(file.read_bytes())},
        }
    from agent_brain_client.adapters.installation.lifecycle import (
        read_removal,
        retained_memory_ids,
        retained_mcp_enabled,
    )

    journal = read_removal(home)
    return {
        "metadata": journal["registration"],
        "installed": False,
        "service_enabled": retained_mcp_enabled(journal, home),
        "memory_ids": retained_memory_ids(journal, home),
        "source": {
            str(root / "uninstall-receipt.json"): io.digest(
                (root / "uninstall-receipt.json").read_bytes()
            )
        },
    }
