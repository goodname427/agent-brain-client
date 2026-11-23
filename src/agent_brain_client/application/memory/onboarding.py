"""One explicitly authorized batch of personal native-memory originals."""

import re
from collections import Counter

from agent_brain_client.domain.service import endpoint_check


TOOL_NAME = "memory_onboard"
SOURCE_SCOPE = "shareable-personal-native-memory"

INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "harness": {"enum": ["codex", "codebuddy"]},
        "destination": {
            "type": "object",
            "properties": {
                "endpoint": {"type": "string"},
                "actor": {"type": "string"},
                "origin": {"type": "string", "description": "Exact approved local origin directory."},
            },
            "required": ["endpoint", "actor", "origin"],
            "additionalProperties": False,
        },
        "source_scope": {
            "const": SOURCE_SCOPE,
            "description": "The current host's supported personal native-memory scope, explicitly confirmed shareable by the user.",
        },
        "approved": {
            "const": True,
            "description": "Required to start a batch. The user approved this shareable source scope and exact destination.",
        },
        "resume": {
            "type": "boolean",
            "description": "Return progress and resume only an already approved batch at this destination, using its retained source snapshot.",
        },
        "approval_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
    },
    "required": ["harness", "destination", "source_scope"],
    "additionalProperties": False,
}

def authorization(arguments):
    if (
        not isinstance(arguments, dict)
        or set(arguments) - {"harness", "destination", "source_scope", "approved", "resume", "approval_id"}
        or arguments.get("source_scope") != SOURCE_SCOPE
        or arguments.get("harness") not in ("codex", "codebuddy")
        or type(arguments.get("resume", False)) is not bool
    ):
        raise ValueError("Explicitly approved native-memory batch required")
    destination = arguments.get("destination")
    if not isinstance(destination, dict) or set(destination) != {"endpoint", "actor", "origin"}:
        raise ValueError("Exact already-approved destination required")
    actor, origin = destination["actor"], destination["origin"]
    endpoint = endpoint_check(destination["endpoint"])
    if (
        not isinstance(actor, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", actor)
        or not isinstance(origin, str)
        or not 0 < len(origin) <= 4096
        or any(ord(char) < 32 for char in origin)
    ):
        raise ValueError("Exact already-approved destination required")
    resume = arguments.get("resume", False)
    approval_id = arguments.get("approval_id")
    if (
        ("approved" in arguments and arguments["approved"] is not True)
        or (not resume and arguments.get("approved") is not True)
        or ("approval_id" in arguments and (
            not resume or not isinstance(approval_id, str) or not re.fullmatch(r"[a-f0-9]{64}", approval_id)
        ))
    ):
        raise ValueError("Start requires scope approval. Resume accepts only a retained approval ID.")
    return arguments["harness"], endpoint, actor, origin, resume, approval_id


class Summary:
    def __init__(self, harness):
        self.harness = harness
        self.counts = dict.fromkeys(("received", "already_exists", "excluded", "pending", "failed"), 0)
        self.reasons: Counter[str] = Counter()
        self.extraction: Counter[str] = Counter()

    def add(self, outcome, reason=None, response=None):
        self.counts[outcome] += 1
        if reason is not None:
            self.reasons[reason] += 1
        if outcome in ("received", "already_exists"):
            state = (response or {}).get("result", {}).get("capture_extraction", {}).get("state")
            if state not in ("succeeded", "staged", "pending", "queued", "waiting", "running", "no-facts",
                             "failed", "blocked", "paused", "review-required", "coalesced"):
                state = "unknown"
            self.extraction[state] += 1

    def result(self, inventory):
        incomplete = (
            inventory != "complete"
            or self.counts["pending"] > 0
            or self.counts["failed"] > 0
            or self.reasons["project-origin-unverified"] > 0
        )
        return {
            "status": "onboarding-incomplete" if incomplete else "onboarding-processed",
            "harness": self.harness,
            "inventory": inventory,
            "counts": self.counts,
            "reasons": dict(self.reasons),
            "extraction": dict(self.extraction),
            "account_complete": False,
        }
