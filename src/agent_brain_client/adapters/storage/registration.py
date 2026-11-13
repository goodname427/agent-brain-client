"""Read the existing enrolled service binding without activating secrets.

The persisted filename is a device schema field, not a Harness implementation.
"""

from agent_brain_client.adapters.storage.metadata import private_json, canonical
from pathlib import Path


def codex_registration(home):
    """Read the public native binding record without loading a host adapter."""
    directory = canonical(Path(home) / ".config/agent-brain-client/codex")
    file = directory / "registration.json"
    if not file.exists():
        return None
    value = private_json(file)
    if (set(value) != {"schema", "native_home", "mcp", "removed"}
            or value["schema"] != 1 or type(value["removed"]) is not bool
            or not isinstance(value["mcp"], dict)):
        raise ValueError("Exact Codex native registration required")
    native = canonical(Path(value["native_home"]))
    if not native.is_absolute() or native == directory.parent / "shared-native":
        raise ValueError("Separate Codex native home required")
    return value


def content_location(home, runtime, harness):
    """Resolve authenticated persisted locations; never initialize a Harness."""
    home = canonical(home)
    base = home / ".config/agent-brain-client"
    if harness == "codebuddy":
        return home / ".codebuddy", base / "codebuddy/content-cache"
    if harness != "codex":
        raise ValueError("Explicit supported content Harness required")
    if Path(runtime["codex_home"]) != base / "shared-native":
        # Original enrolled schema retains its authenticated direct locations.
        return canonical(Path(runtime["codex_home"])), Path(runtime["content_cache"])
    receipt = codex_registration(home)
    if receipt is None:
        raise ValueError("Explicit Codex native binding required")
    return canonical(Path(receipt["native_home"])), base / "codex/content-cache"


def registry_home(home, *, retained=False):
    """Locate shared control data independently of any installed native host.

    The original location remains authenticated recovery data until structural
    reinstall detaches it. Newly prepared control data always lives privately.
    """
    from agent_brain_client.adapters.storage.reinstallation import read_journal
    home = canonical(home)
    fresh = read_journal(home)
    if fresh is not None:
        if retained or fresh["phase"] not in ("ready", "complete"):
            return canonical(Path(fresh["native"]))
        return home / ".config/agent-brain-client/shared-native"
    file = home / ".config/agent-brain-client/codex-mcp.json"
    if file.exists() or file.is_symlink():
        metadata = private_json(file)
        policy = private_json(metadata["release_policy"], metadata["release_policy_sha256"])
        return canonical(Path(policy["runtime"]["codex_home"]))
    removal = home / ".config/agent-brain-client/uninstall-receipt.json"
    if removal.exists():
        metadata = private_json(removal)["registration"]
        policy = private_json(metadata["release_policy"], metadata["release_policy_sha256"])
        return canonical(Path(policy["runtime"]["codex_home"]))
    return home / ".config/agent-brain-client/shared-native"


def registration(home):
    m = private_json(canonical(home) / ".config/agent-brain-client/codex-mcp.json")
    policy = private_json(m["release_policy"], m["release_policy_sha256"])
    runtime = policy["runtime"]
    if policy["capabilities"] != [
        "codex-stdio-mcp",
        "personal-content-explicit",
        "https-system-credential",
    ]:
        raise ValueError("Existing HTTPS content registration required")
    private_json(runtime["https_profile"], runtime["https_profile_sha256"])
    private_json(runtime["content_policy"], runtime["content_policy_sha256"])
    return m, policy
