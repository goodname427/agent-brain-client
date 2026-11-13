"""Explicit Codex content ports."""

import os
from pathlib import Path
from agent_brain_client.adapters.harnesses.codex.mapping import CodexContentMapping
from agent_brain_client.adapters.harnesses.codex.memory_inventory import CodexMemorySources


class CodexContentPorts:
    name = "codex"
    mapping = CodexContentMapping
    sources = CodexMemorySources
    protected = (".codex", ".agents", ".config/agent-brain-client/content")

    @staticmethod
    def default_home(home):
        return os.environ.get("CODEX_HOME", str(Path(home) / ".codex"))

    @staticmethod
    def binding(home, native):
        return {"device_home": str(home), "codex_home": str(native)}

    @staticmethod
    def location(home, runtime):
        from agent_brain_client.adapters.harnesses.codex.binding import location
        return location(home, runtime)

    @staticmethod
    def remove_hooks(home, raw, platform):
        from agent_brain_client.adapters.harnesses.codex.collection_installation import split_owned, same_owned, read_receipt
        from agent_brain_client.adapters.storage.collection import settings_path
        outside, block = split_owned(raw)
        if block is None:
            return raw, None
        receipt = read_receipt(settings_path(home).parent / "hook-install.json")
        if receipt.get("phase") != "complete" or not same_owned(receipt.get("block"), block):
            raise ValueError("Hook ownership changed")
        return outside.encode(), block

    @staticmethod
    def detach_payload(home, native, metadata, *, installed=True):
        from agent_brain_client.adapters.harnesses.codex.installation_detach import detach_payload
        return detach_payload(home, native, metadata, installed=installed)
