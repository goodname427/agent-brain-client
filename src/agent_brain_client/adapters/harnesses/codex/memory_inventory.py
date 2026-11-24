"""Codex persistent-memory source contract; sessions are outside its scope."""

from pathlib import Path
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
