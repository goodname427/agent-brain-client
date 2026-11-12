"""Explicit CodeBuddy content ports."""

from pathlib import Path
from agent_brain_client.adapters.harnesses.codebuddy.mapping import CodeBuddyContentMapping
from agent_brain_client.adapters.harnesses.codebuddy.sources import CodeBuddyMemorySources


class CodeBuddyContentPorts:
    name = "codebuddy"
    mapping = CodeBuddyContentMapping
    sources = CodeBuddyMemorySources
    protected = (".codebuddy", ".config/agent-brain-client/content")

    @staticmethod
    def default_home(home):
        return Path(home) / ".codebuddy"

    @staticmethod
    def binding(home, native):
        return {"device_home": str(home), "host": "codebuddy", "native_home": str(native)}

    @staticmethod
    def location(home, runtime):
        return Path(home) / ".codebuddy", Path(home) / ".config/agent-brain-client/codebuddy/content-cache"

    @staticmethod
    def remove_hooks(home, raw, platform):
        return raw, None

    @staticmethod
    def detach_payload(home, native, metadata, *, installed=True):
        from agent_brain_client.adapters.installation.lifecycle import remove_table
        from agent_brain_client.adapters.installation.installation import registered_mcp_digest
        from agent_brain_client.adapters.installation import registry_format
        from agent_brain_client.adapters.harnesses.codebuddy.configuration import fresh_installation_payload
        file = native / "config.toml"
        before = file.read_bytes() if file.exists() else b""
        mcp = registry_format.tomllib.loads(before.decode()).get("mcp_servers", {}).get(registry_format.NAME)
        if installed and (not isinstance(mcp, dict) or registered_mcp_digest(mcp) != metadata["mcp_sha256"]):
            raise ValueError("Owned Client launcher changed")
        if not installed and mcp is not None:
            raise ValueError("Removed Client launcher changed")
        return {file: remove_table(before) if installed else before, **fresh_installation_payload(home)}
