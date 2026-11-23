"""One explicitly authorized batch of personal native-memory originals."""

import re
from collections import Counter

from agent_brain_client.domain.events import MAX_NATIVE_MEMORY_UNITS
from agent_brain_client.domain.privacy import private_recall_text
from agent_brain_client.domain.service import endpoint_check


TOOL_NAME = "memory_onboard"

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
        "source_fingerprints": {
            "type": "array",
            "items": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "maxItems": 1024,
            "uniqueItems": True,
            "description": "Fingerprints of originals the user explicitly approves as shareable personal memory for this destination. Omit to resume only previously approved requests already bound to this origin.",
        },
        "approved": {
            "const": True,
            "description": "The user explicitly approved importing this host's shareable personal originals to the exact destination.",
        },
    },
    "required": ["harness", "destination", "approved"],
    "additionalProperties": False,
}

# Exclude recognizable credentials before constructing a network request. Exact
# original approval is independently required for unmarked sensitive material.
_CREDENTIAL = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
    r"|\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{12,}"
    r"|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,})\b"
    r"|\b(?:authorization\s*:\s*bearer|(?:api[-_ ]?key|access[-_ ]?key|"
    r"client[-_ ]?secret|access[-_ ]?token|refresh[-_ ]?token|password|passwd|"
    r"credential|secret|令牌|密码|密钥)\s*[:=])\s*[\x22\x27]?[A-Za-z0-9_./+@!#$%^&*=:-]{4,}"
    r"|\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@",
    re.IGNORECASE,
)
_CONFIDENTIAL = re.compile(
    r"(?im)^\s*(?:\[(?:confidential|secret|internal[-_ ]?only)\]"
    r"|(?:confidential|internal[-_ ]?only|do not distribute|机密|保密)\s*[:：])"
)


def authorization(arguments):
    if (
        not isinstance(arguments, dict)
        or set(arguments) - {"harness", "destination", "source_fingerprints", "approved"}
        or arguments.get("approved") is not True
        or arguments.get("harness") not in ("codex", "codebuddy")
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
    fingerprints = arguments.get("source_fingerprints", [])
    if (
        not isinstance(fingerprints, list)
        or len(fingerprints) > 1024
        or any(not isinstance(item, str) or not re.fullmatch(r"[a-f0-9]{64}", item)
               for item in fingerprints)
        or len(fingerprints) != len(set(fingerprints))
    ):
        raise ValueError("Exact original fingerprints required")
    return arguments["harness"], endpoint, actor, origin, frozenset(fingerprints)


def excluded_original(body):
    if private_recall_text(body) or _CONFIDENTIAL.search(body) or _CREDENTIAL.search(body):
        return "private-or-sensitive"
    if not body.strip() or "\0" in body or len(body.encode("utf-16-le")) // 2 > MAX_NATIVE_MEMORY_UNITS:
        return "original-outside-bound"
    return None


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
            or any(self.reasons[key] for key in (
                "shareability-unconfirmed", "project-origin-unverified", "legacy-receipt-unconfirmed",
            ))
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
