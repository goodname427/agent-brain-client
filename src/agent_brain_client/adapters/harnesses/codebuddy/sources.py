"""CodeBuddy persistent auto-memory and user memory; no transcript discovery."""

from collections import Counter
from contextlib import closing
import os

from agent_brain_client.adapters.platforms.source_files import open_native_source
from agent_brain_client.adapters.platforms.tree import file_version
from agent_brain_client.adapters.storage.content_limits import sha
from agent_brain_client.adapters.storage.native_sources import (
    read_selected_source, selected_tree_sources,
)
from agent_brain_client.domain import contract
from agent_brain_client.domain.errors import CacheBlocked


_NON_MEMORY_DIRECTORIES = {".git", "sessions", "history", "transcripts"}


def _memory_tree(relative, directory):
    name = relative.rsplit("/", 1)[-1].casefold()
    if directory:
        return name not in _NON_MEMORY_DIRECTORIES
    # MEMORY.md is the native project memory index. JSON state, migration
    # markers, session logs and backup files are outside the memory formats.
    return name.endswith((".md", ".mdc")) and name != "memory.md"


def _project_memory(relative, directory):
    parts = relative.casefold().split("/")
    if len(parts) == 1:
        return directory
    if len(parts) == 2:
        return directory and parts[1] == "memory"
    return parts[1] == "memory" and _memory_tree(relative, directory)


class CodeBuddyMemorySources:
    prefix = "codebuddy-memory:"

    def __init__(self, session):
        self.home = session.native_memory_home
        device = self.device_home = session.home
        windows = session.platform.startswith("win32-")
        local_data = device / ("AppData/Local" if windows else "Library/Application Support")
        self.ide = local_data / "CodeBuddyExtension/Data/Public/.memories"
        self._trees = [
            (self.home / "memories", "cli/", _memory_tree),
            (self.ide, "ide/", _memory_tree),
        ]
        if windows:
            self._trees.extend(
                [
                    (self.home / "memery", "memery/", _memory_tree),
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

    def _source_id(self, name):
        return self.prefix + sha(name.encode("utf-8", "surrogateescape"))

    def snapshot_rows(self, *, wanted=None):
        """Stream path-free metadata plus a private, namespace-bound locator."""
        for root, namespace, select in self._trees:
            tree = selected_tree_sources(
                root, source_id=lambda relative: self._source_id(namespace + relative),
                select=select, wanted=wanted,
            )
            with closing(tree):
                for status, source in tree:
                    if source is None:
                        yield status, None
                        continue
                    relative, row = source
                    if row["status"] == "not-checked":
                        row["locator"] = [namespace, relative.encode("utf-8", "surrogateescape").hex()]
                    yield status, row
        user_id = self._source_id("CODEBUDDY.md")
        if wanted is not None and wanted != user_id:
            return
        try:
            raw = self._user()
            if raw.strip():
                yield "complete", {
                    "source_id": user_id, "source_fingerprint": sha(raw), "status": "not-checked",
                    "reason": "fingerprinted; semantic review required",
                }
        except FileNotFoundError:
            pass
        except (OSError, ValueError, CacheBlocked):
            yield "incomplete", {
                "source_id": user_id, "source_fingerprint": None, "status": "unavailable",
                "reason": "user memory changed or unavailable",
            }

    @staticmethod
    def inventory_status(statuses):
        if "incomplete" in statuses or statuses == {"complete", "unavailable"}:
            return "incomplete"
        if "complete" in statuses:
            return "complete"
        return "unavailable"

    def inventory(self, *, onboarding=False):
        rows, statuses = [], set()
        for status, row in self.snapshot_rows():
            statuses.add(status)
            if row is not None:
                row.pop("locator", None)
                rows.append(row)
        rows.sort(key=lambda row: row["source_id"])
        status = self.inventory_status(statuses)
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

    def read(self, source_id, *, with_mtime=False, locator=None):
        if source_id == self._source_id("CODEBUDDY.md"):
            return self._user(with_mtime=with_mtime)
        if locator is None:
            # Earlier approvals persisted only the ID and fingerprint.
            with closing(self.snapshot_rows(wanted=source_id)) as rows:
                locator = next(
                    (row["locator"] for _, row in rows
                     if row and row["source_id"] == source_id and "locator" in row),
                    None,
                )
        if (
            not isinstance(locator, list)
            or len(locator) != 2
            or any(not isinstance(part, str) for part in locator)
        ):
            raise CacheBlocked()
        namespace, encoded = locator
        relative = bytes.fromhex(encoded).decode("utf-8", "surrogateescape")
        if self._source_id(namespace + relative) != source_id:
            raise CacheBlocked()
        for root, name, select in self._trees:
            if name == namespace:
                return read_selected_source(root, relative, select=select, with_mtime=with_mtime)
        raise CacheBlocked()
