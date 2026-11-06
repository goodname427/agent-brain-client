"""Detach authenticated Codex entries while retaining every memory projection."""

import json
from pathlib import Path
from agent_brain_client.adapters.storage import metadata as io
from agent_brain_client.adapters.harnesses.codex import configuration as config
from agent_brain_client.adapters.harnesses.codex.installation import (
    registered_mcp_digest,
)
from agent_brain_client.adapters.harnesses.codex.collection_installation import (
    split_owned,
    same_owned,
)
from agent_brain_client.adapters.harnesses.codex.lifecycle import remove_table
from agent_brain_client.adapters.harnesses.codex.content import CodexContentSession


def mapping_view(home, native, cache, content):
    from agent_brain_client.adapters.platforms.filesystem import native_platform

    return CodexContentSession.metadata_view(
        home, native, cache, content, platform=native_platform()
    ).mapping


def retained_mapping(home, native, cache, content):
    mapping = mapping_view(home, native, cache, content)
    if not mapping.transaction_settled():
        raise ValueError("Resolve pending native mapping before fresh installation")
    return mapping.retention_snapshot()


def restore_mapping(home, native, cache, content, snapshot):
    mapping_view(home, native, cache, content).restore_retention(snapshot)


def detach_payload(home, native, metadata, *, installed=True):
    file = io.canonical(native / "config.toml", existing=installed)
    before = file.read_bytes() if file.exists() else b""
    mcp = config.tomllib.loads(before.decode()).get("mcp_servers", {}).get(config.NAME)
    if installed and (
        not isinstance(mcp, dict)
        or registered_mcp_digest(mcp) != metadata["mcp_sha256"]
    ):
        raise ValueError("Owned Agent Brain MCP entry changed")
    if not installed and mcp is not None:
        raise ValueError("Uninstalled registration has a new native entry; preserve it")
    outside, block = split_owned(before)
    if block is not None:
        receipt = io.private_json(
            home / ".config/agent-brain-client/global-memory/hook-install.json"
        )
        if receipt.get("phase") != "complete" or not same_owned(
            block, receipt.get("block")
        ):
            raise ValueError("Global Hook receipt changed")
    payload = {file: remove_table(outside.encode()) if installed else outside.encode()}
    base = home / ".config/agent-brain-client/project-memory"
    if base.exists():
        receipts = list(base.glob("*/hooks-install-*.json"))
        if len(receipts) > 1024:
            raise ValueError("Bounded project Hook inventory required")
        for path in receipts:
            receipt = io.private_json(path)
            settings = io.private_json(
                path.parent / "settings.json", receipt["settings_sha256"]
            )
            target = io.canonical(Path(settings["project_root"]) / ".codex/hooks.json")
            if (
                settings["device_home"] != str(home)
                or receipt.get("phase") != "complete"
                or receipt["target"] != str(target)
            ):
                raise ValueError("Project Hook scope changed")
            if not target.exists():
                continue
            value = json.loads(target.read_bytes(), object_pairs_hook=io.unique)
            for event, group in receipt["definitions"].items():
                groups = value.get("hooks", {}).get(event, [])
                if groups.count(group) != 1:
                    raise ValueError(
                        "Project Hook changed; preserve native configuration"
                    )
                groups.remove(group)
            payload[target] = (
                json.dumps(value, ensure_ascii=False, indent=2) + "\n"
            ).encode()
    return payload


def retained_memory_ids(snapshot):
    return sorted(
        {
            Path(name).stem
            for name in snapshot["owned"]["files"]
            if name.startswith(".config/agent-brain-client/content/memory/")
        }
    )


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
    from agent_brain_client.adapters.harnesses.codex.lifecycle import (
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
