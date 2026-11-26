"""Codex persistent-memory source contract; sessions are outside its scope."""

from pathlib import Path, PurePosixPath
import re
from agent_brain_client.adapters.storage.native_sources import (
    inventory_tree, read_selected_source, read_tree, snapshot_locator, snapshot_tree,
)
from agent_brain_client.domain.errors import CacheBlocked


def _memory_source(relative, _directory):
    # Prune Git directories and worktree marker files before touching the entry.
    return relative.rsplit("/", 1)[-1].casefold() != ".git"


def inventory_codex_memories(codex_home, **limits):
    return inventory_tree(
        Path(codex_home) / "memories",
        prefix="codex-memory:",
        select=_memory_source,
        **limits,
    )


class CodexMemorySources:
    prefix = "codex-memory:"

    def __init__(self, session):
        self.root = session.native_memory_home / "memories"

    def inventory(self):
        return inventory_tree(self.root, prefix=self.prefix, select=_memory_source)

    def snapshot_rows(self):
        return snapshot_tree(self.root, prefix=self.prefix, namespace="", select=_memory_source)

    def onboarding_workspace(self, source_id, locator, body):
        if locator is None:
            return None
        namespace, relative = snapshot_locator(self.prefix, source_id, locator)
        parts = PurePosixPath(relative).parts
        if namespace or len(parts) != 2 or parts[0] != "rollout_summaries" or not parts[1].endswith(".md"):
            return None

        # Codex writes this header before the generated markdown. Never read
        # the named rollout, or look for a workspace in the summary text.
        head, boundary, _ = body[:8192].replace("\r\n", "\n").partition("\n\n")
        if not boundary:
            return None
        lines = head.split("\n")
        if (
            len(lines) not in (4, 5)
            or not re.fullmatch(r"thread_id: [a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", lines[0])
            or not re.fullmatch(r"updated_at: \d{4}-\d{2}-\d{2}T[^\s]+", lines[1])
            or not lines[2].startswith("rollout_path: ")
            or not lines[3].startswith("cwd: ")
            or len(lines) == 5 and not lines[4].startswith("git_branch: ")
        ):
            return None
        return lines[3][5:]

    def read(self, source_id, *, with_mtime=False, locator=None):
        if locator is not None:
            namespace, relative = snapshot_locator(self.prefix, source_id, locator)
            if namespace:
                raise CacheBlocked()
            return read_selected_source(self.root, relative, select=_memory_source, with_mtime=with_mtime)
        return read_tree(
            self.root, source_id, prefix=self.prefix,
            select=_memory_source, with_mtime=with_mtime,
        )
