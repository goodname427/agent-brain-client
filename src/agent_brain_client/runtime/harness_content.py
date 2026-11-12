"""Resolve an explicitly selected native content port bundle."""


def content_host(harness):
    if harness == "codex":
        from agent_brain_client.adapters.harnesses.codex.content_ports import CodexContentPorts
        return CodexContentPorts
    if harness == "codebuddy":
        from agent_brain_client.adapters.harnesses.codebuddy.content_ports import CodeBuddyContentPorts
        return CodeBuddyContentPorts
    raise ValueError("Explicit supported content Harness required")


def content_location(home, runtime, harness):
    return content_host(harness).location(home, runtime)


def event_hook_host(harness):
    """The finite MCP event-hook protocol is currently implemented by Codex only.

    CodeBuddy uses its managed native events entry, including transcript pairing
    and deferred completion; it must never interpret the Codex event protocol.
    """
    if harness == "codex":
        from agent_brain_client.adapters.harnesses.codex import hooks
        return hooks
    raise ValueError("Harness does not support the MCP event-hook protocol; use its managed native Hook entry")
