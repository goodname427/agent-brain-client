"""Codex limits: concrete native behavior, separate from shared use cases."""

import hashlib
import re

BEGIN = "<!-- agent-brain-content:begin -->"
END = "<!-- agent-brain-content:end -->"


BEGIN = "<!-- agent-brain-content:begin -->"
END = "<!-- agent-brain-content:end -->"


BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"(?:\r?\n)?", re.S)


OFFLINE_TRANSPORT_ERRORS = frozenset(
    ("transport_unconfirmed", "ssh_proxy_unconfirmed", "server_proxy_unconfirmed")
)


SNAPSHOT_BYTES = 256 * (65536 + 512)


MAPPING_BYTES = 2 * SNAPSHOT_BYTES + 262144


TRANSACTION_BYTES = 8 * MAPPING_BYTES


REVIEW_DECISIONS = {
    "shareable": {"safe-cross-project-personal-summary"},
    "excluded": {"secret-localonly-or-confidential"},
    "deferred": {"project-scope-unavailable", "uncertain-content"},
    "skipped": {"no-durable-user-fact"},
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def cache_bytes(name):
    if name == "transaction.json":
        return TRANSACTION_BYTES
    if name == "owned.json" or re.fullmatch(r"snapshots/[a-f0-9]{40}\.json", name):
        return SNAPSHOT_BYTES
    return 1048576
