"""Codex persistent-memory source contract; sessions are outside its scope."""

from pathlib import Path
from agent_brain_client.adapters.storage.native_sources import inventory_tree, read_tree


def inventory_codex_memories(codex_home, **limits):
    return inventory_tree(Path(codex_home) / "memories", prefix="codex-memory:", **limits)


class CodexMemorySources:
    prefix = "codex-memory:"

    def __init__(self, session):
        self.root = session.native_memory_home / "memories"

    def inventory(self):
        return inventory_tree(self.root, prefix=self.prefix)

    def read(self, source_id):
        return read_tree(self.root, source_id, prefix=self.prefix)
