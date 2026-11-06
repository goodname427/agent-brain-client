"""Read the existing enrolled service binding without activating secrets.

The persisted filename is a device schema field, not a Harness implementation.
"""

from agent_brain_client.adapters.storage.metadata import private_json, canonical


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
