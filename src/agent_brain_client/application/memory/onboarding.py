"""One explicitly authorized batch of personal native-memory originals."""

import re
from collections import Counter

from agent_brain_client.domain.events import MAX_NATIVE_MEMORY_BYTES
from agent_brain_client.domain.privacy import private_recall_text
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

# The user confirms the shareable source scope. Known credentials and local
# restrictions still exclude exact originals before any network submission.
_CREDENTIAL = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
    r"|\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{12,}"
    r"|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,})\b"
    r"|\bauthorization\s*:\s*bearer\s+[A-Za-z0-9_./+=-]{4,}"
    r"|(?:^|[\s{,;])[\x22\x27]?(?:[a-z][a-z0-9]*[-_])*(?:api[-_ ]?key|"
    r"access[-_ ]?key|client[-_ ]?secret|access[-_ ]?token|refresh[-_ ]?token|"
    r"password|passwd|credential|secret|token|令牌|密码|密钥)[\x22\x27]?\s*[:=]"
    r"\s*[\x22\x27]?[A-Za-z0-9_./+@!#$%^&*=:-]{4,}"
    r"|\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@",
    re.IGNORECASE,
)
_CONFIDENTIAL = re.compile(
    r"(?im)^\s*(?:\[(?:confidential|secret|internal[-_ ]?only)\]"
    r"|(?:confidential|internal[-_ ]?only|do not distribute|机密|保密)\s*[:：])"
)
_UNVERIFIED_SCOPE = re.compile(
    r"(?im)^\s*(?:(?:sharing|scope)\s*:\s*(?:unknown|unverified|project|private)\b"
    r"|\[(?:unverified|unreviewed)\])"
)


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


def excluded_original(body):
    if private_recall_text(body) or _CONFIDENTIAL.search(body) or _CREDENTIAL.search(body):
        return "private-or-sensitive"
    if _UNVERIFIED_SCOPE.search(body):
        return "shareability-unconfirmed"
    if not body.strip() or "\0" in body or len(body.encode("utf-8")) > MAX_NATIVE_MEMORY_BYTES:
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
                "shareability-unconfirmed", "project-origin-unverified",
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
