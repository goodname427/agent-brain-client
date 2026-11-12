"""Resolve an explicitly selected native content port bundle."""


def content_host(harness):
    if harness == "codex":
        from agent_brain_client.adapters.harnesses.codex.content_ports import CodexContentPorts
        return CodexContentPorts
    if harness == "codebuddy":
        from agent_brain_client.adapters.harnesses.codebuddy.content_ports import CodeBuddyContentPorts
        return CodeBuddyContentPorts
    raise ValueError("Explicit supported content Harness required")
