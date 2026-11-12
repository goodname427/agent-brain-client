"""Detach only authenticated Codex native MCP and project Hook entries."""

import json
from pathlib import Path
from agent_brain_client.adapters.storage import metadata as io
from agent_brain_client.adapters.installation import registry_format as config
from agent_brain_client.adapters.installation.installation import registered_mcp_digest
from agent_brain_client.adapters.harnesses.codex.collection_installation import split_owned, same_owned
from agent_brain_client.adapters.installation.lifecycle import remove_table


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
