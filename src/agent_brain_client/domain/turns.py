"""Stable turn identity and text contract; no storage or native host."""

import hashlib
from agent_brain_client.domain import contract


def normalized_turn(scope, turn, phase, content):
    """Validate normalized turn text and retain the existing deterministic key."""
    if phase not in ("prompt", "final"):
        raise ValueError("Expected prompt or final phase")
    contract.event_identifier(scope["project_id"])
    contract.event_identifier(scope["session_id"])
    contract.event_identifier(turn)
    contract.text(content, 32768)
    if len(content.encode("utf-16-le")) // 2 > 8192:
        raise ValueError("Native text exceeds bound")
    return hashlib.sha256(contract.encode([scope, turn])).hexdigest()
