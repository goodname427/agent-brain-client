"""CodeBuddy persistent auto-memory and user memory; no transcript discovery."""

from agent_brain_client.adapters.storage.native_sources import inventory_tree, read_tree
from agent_brain_client.adapters.storage.content_limits import sha
from agent_brain_client.adapters.platforms.source_files import open_native_source
from agent_brain_client.domain import contract
from agent_brain_client.domain.errors import CacheBlocked


class CodeBuddyMemorySources:
    prefix = "codebuddy-memory:"

    def __init__(self, session):
        self.home = session.native_memory_home
        device = session.home
        self.ide = (device / "AppData/Local" if session.platform.startswith("win32-") else device / "Library/Application Support") / "CodeBuddyExtension/Data/Public/.memories"

    def _user(self):
        stream, info = open_native_source(self.home / "CODEBUDDY.md")
        with stream:
            if info.st_size > 131072:
                raise CacheBlocked()
            raw = stream.read(131073)
        if len(raw) > 131072:
            raise CacheBlocked()
        # The owned connection guidance contains no user memory. Never import it.
        from agent_brain_client.adapters.harnesses.codebuddy.configuration import GUIDANCE
        return raw.replace(GUIDANCE.encode(), b"")

    def inventory(self):
        value = inventory_tree(self.home / "memories", prefix=self.prefix, identity_namespace="cli/")
        ide = inventory_tree(self.ide, prefix=self.prefix, identity_namespace="ide/")
        rows = list(value["sources"]) + list(ide["sources"])
        if ide["status"] != "unavailable":
            value = dict(value, status="incomplete" if "incomplete" in (value["status"], ide["status"]) else "complete")
        try:
            raw = self._user()
            if raw.strip():
                rows.append({"source_id": self.prefix + sha(b"CODEBUDDY.md"),
                    "source_fingerprint": sha(raw), "status": "not-checked",
                    "reason": "fingerprinted; semantic review required"})
                if value["status"] == "unavailable":
                    value = dict(value, status="complete")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, CacheBlocked):
            rows.append({"source_id": self.prefix + sha(b"CODEBUDDY.md"),
                "source_fingerprint": None, "status": "unavailable",
                "reason": "user memory changed or unavailable"})
            value = dict(value, status="incomplete")
        if len(rows) > 1024:
            raise CacheBlocked()
        rows.sort(key=lambda row: row["source_id"])
        return dict(value, sources=rows, cursor=sha(contract.encode(rows)))

    def read(self, source_id):
        if source_id == self.prefix + sha(b"CODEBUDDY.md"):
            return self._user()
        try:
            return read_tree(self.home / "memories", source_id, prefix=self.prefix, identity_namespace="cli/")
        except (OSError, CacheBlocked):
            return read_tree(self.ide, source_id, prefix=self.prefix, identity_namespace="ide/")
