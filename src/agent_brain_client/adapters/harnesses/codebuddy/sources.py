"""CodeBuddy persistent auto-memory and user memory; no transcript discovery."""

from collections import Counter
import os

from agent_brain_client.adapters.platforms.source_files import open_native_source
from agent_brain_client.adapters.platforms.tree import file_version
from agent_brain_client.adapters.storage.content_limits import sha
from agent_brain_client.adapters.storage.native_sources import inventory_tree, read_tree
from agent_brain_client.domain import contract
from agent_brain_client.domain.errors import CacheBlocked


def _legacy_memory(relative, directory):
    name = relative.rsplit("/", 1)[-1].casefold()
    if directory:
        return name not in {"sessions", "history", "transcripts"}
    return name.endswith((".md", ".mdc")) and name != "memory.md"


def _global_memory(relative, directory):
    return (
        not directory
        and "/" not in relative
        and relative.casefold().endswith("_memery.md")
    )


def _project_memory(relative, directory):
    parts = relative.casefold().split("/")
    if directory:
        return len(parts) == 1 or (len(parts) == 2 and parts[1] == "memory")
    return (
        len(parts) == 3
        and parts[1] == "memory"
        and parts[2].endswith(".md")
        and parts[2] != "memory.md"
    )


class CodeBuddyMemorySources:
    prefix = "codebuddy-memory:"

    def __init__(self, session):
        self.home = session.native_memory_home
        device = self.device_home = session.home
        windows = session.platform.startswith("win32-")
        local_data = device / ("AppData/Local" if windows else "Library/Application Support")
        self.ide = local_data / "CodeBuddyExtension/Data/Public/.memories"
        self._trees = [
            (self.home / "memories", "cli/", _legacy_memory),
            (self.ide, "ide/", _legacy_memory),
        ]
        if windows:
            self._trees.extend(
                [
                    (self.home / "memery", "memery/", _global_memory),
                    (self.home / "projects", "projects/", _project_memory),
                ]
            )

    def _user(self, *, with_mtime=False):
        stream, info = open_native_source(self.home / "CODEBUDDY.md")
        with stream:
            if info.st_size > 131072:
                raise CacheBlocked()
            raw = stream.read(131073)
            if file_version(info) != file_version(os.fstat(stream.fileno())):
                raise CacheBlocked()
        if len(raw) > 131072:
            raise CacheBlocked()
        # The owned connection guidance contains no user memory. Never import it.
        from agent_brain_client.adapters.harnesses.codebuddy.configuration import (
            BEGIN, GUIDANCE, guidance_forms, recorded_guidance, state_directory,
        )
        from agent_brain_client.adapters.storage import metadata

        user = raw
        for block in guidance_forms(GUIDANCE):
            user = user.replace(block.encode(), b"")
        if user == raw and BEGIN.encode() in raw:
            journal = state_directory(self.device_home) / "transaction.json"
            if journal.exists():
                block = recorded_guidance(metadata.private_json(journal), self.home / "CODEBUDDY.md")
                if block is not None:
                    for form in guidance_forms(block):
                        user = user.replace(form.encode(), b"")
        return (user, info.st_mtime_ns) if with_mtime else user

    def inventory(self, *, onboarding=False):
        rows = []
        statuses = set()
        for root, namespace, select in self._trees:
            tree = inventory_tree(
                root, prefix=self.prefix, identity_namespace=namespace,
                select=select, optional=True,
                excluded_reason="project-origin-unverified"
                if onboarding and namespace == "projects/" else None,
            )
            if tree is not None:
                rows.extend(tree["sources"])
                statuses.add(tree["status"])
        try:
            raw = self._user()
            if raw.strip():
                rows.append({"source_id": self.prefix + sha(b"CODEBUDDY.md"),
                    "source_fingerprint": sha(raw), "status": "not-checked",
                    "reason": "fingerprinted; semantic review required"})
                statuses.add("complete")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, CacheBlocked):
            rows.append({"source_id": self.prefix + sha(b"CODEBUDDY.md"),
                "source_fingerprint": None, "status": "unavailable",
                "reason": "user memory changed or unavailable"})
            statuses.add("incomplete")
        if len(rows) > 1024:
            raise CacheBlocked()
        rows.sort(key=lambda row: row["source_id"])
        if "incomplete" in statuses or statuses == {"complete", "unavailable"}:
            status = "incomplete"
        elif "complete" in statuses:
            status = "complete"
        else:
            status = "unavailable"
        return {
            "schema": 1,
            "status": status,
            "reason": {
                "complete": "metadata inventory only; no import coverage claimed",
                "incomplete": "one or more memory sources could not be inventoried",
                "unavailable": "no available CodeBuddy memory roots",
            }[status],
            "sources": rows,
            "counts": dict(Counter(row["status"] for row in rows)),
            "cursor": sha(contract.encode(rows)),
        }

    def read(self, source_id, *, with_mtime=False):
        if source_id == self.prefix + sha(b"CODEBUDDY.md"):
            return self._user(with_mtime=with_mtime)
        for root, namespace, select in self._trees:
            try:
                return read_tree(
                    root, source_id, prefix=self.prefix, identity_namespace=namespace,
                    select=select, with_mtime=with_mtime,
                )
            except (OSError, CacheBlocked):
                continue
        raise CacheBlocked()
