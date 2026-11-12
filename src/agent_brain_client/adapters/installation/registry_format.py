"""Persisted launcher registry syntax, preserving existing authenticated bytes."""

import base64
import hashlib
import json
import re
import tomllib
from pathlib import Path
from agent_brain_client.adapters.storage.host_files import host_image
from agent_brain_client.adapters.storage.toml_document import toml_replace as toml_replace, toml_equal as toml_equal

NAME = "agent_brain_server"
TOOLS = ["topic", "recall", "remember", "forget", "client_updates"]
BEGIN = "<!-- agent-brain:begin -->"
END = "<!-- agent-brain:end -->"
BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"(?:\r?\n)?", re.S)
MANAGED = (
    BEGIN
    + "\n# Synthetic test MCP\nUse agent_brain_server only for explicitly requested synthetic tests. Do not automatically query, collect or upload real preferences, facts, rules, memories, project data, native history or credentials. Native memory continues under its existing authorization. Read at most three relevant topics; writes need the read revision and a stable request_id. Only durable=true confirms persistence. Report transport, compatibility and update blockers; do not replace existing permissions or migrate legacy bindings.\n"
    + END
    + "\n"
)
PERSONAL_MANAGED = (
    BEGIN
    + "\n# Explicit personal memory MCP\nUse agent_brain_server only for explicitly authorized personal shared memory operations. Do not automatically collect or upload conversations, native history, local-only/private material, credentials or project data. Native memory remains available. Read revision before writing and retain request_id on retries; only durable=true confirms persistence. A pinned import manifest is required for migration. Do not infer future collection authorization from scans or previously imported topics.\n"
    + END
    + "\n"
)




def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def migration_config_matches(config, current, expected_sha, state, receipt):
    """Recover a legacy semantic baseline from a hash-authenticated image.

    Old migrations recorded only a full-file hash. Existing transaction journals
    may retain that exact image. Its hash authenticates the managed table even
    after unrelated live configuration changes; no current hash is re-pinned.
    """
    if sha(current) == expected_sha:
        return True
    from agent_brain_client.adapters.platforms.filesystem import private_mode

    journals = [receipt]
    for name in (
        "native-install-receipt.json",
        "content-install-receipt.json",
        "global-memory/hook-install.json",
    ):
        path = Path(state) / name
        if not path.exists():
            continue
        try:
            image = host_image(path)
            private_mode(path, owner=True)
            if image is not None:
                journal = json.loads(image[0])
                if isinstance(journal, dict):
                    journals.append(journal)
        except (OSError, ValueError):
            # An unusable unrelated journal is not evidence. Only the exact
            # original image hash can authorize the semantic baseline below.
            continue
    actual = tomllib.loads(current.decode()).get("mcp_servers", {}).get(NAME)
    for journal in journals:
        before = journal.get("before", {}).get(str(config))
        host = journal.get("host", {}).get(str(config), {})
        candidates = [
            before.get("data") if isinstance(before, dict) else None,
            host.get("before"),
        ]
        for encoded in candidates:
            if not isinstance(encoded, str):
                continue
            try:
                original = base64.b64decode(encoded, validate=True)
            except ValueError:
                continue
            if sha(original) != expected_sha:
                continue
            expected = tomllib.loads(original.decode()).get("mcp_servers", {}).get(NAME)
            if not isinstance(expected, dict) or not isinstance(actual, dict):
                return False
            if expected.get("enabled") is True and actual.get("enabled") is False:
                expected = dict(expected, enabled=False)
            return json.dumps(actual, sort_keys=True) == json.dumps(
                expected, sort_keys=True
            )
    return False
