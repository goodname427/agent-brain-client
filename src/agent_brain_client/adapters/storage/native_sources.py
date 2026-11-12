"""Bounded no-follow inventory and reads for a host-selected persistent source tree."""

import hashlib
import itertools
from pathlib import Path
import stat
from agent_brain_client.adapters.platforms.tree import open_tree, is_link
from agent_brain_client.domain import contract
from agent_brain_client.domain.errors import CacheBlocked
from agent_brain_client.adapters.storage.content_limits import sha


def inventory_tree(
    root,
    *,
    prefix,
    identity_namespace="",
    max_files=1024,
    max_file_bytes=131072,
    max_total_bytes=16777216,
    max_entries=1024,
    max_depth=16,
):
    """Fingerprint only the selected persistent memory tree; never inspect sessions.

    The output deliberately contains no source paths or text. Inventoried entries
    remain ``not-checked`` until a separate review classifies and normalizes
    them for import. A digest cursor lets a caller detect a changed inventory
    without treating this scan as coverage or an import plan.
    """
    root = Path(root).expanduser()
    identity_prefix = prefix
    rows = []
    total = 0
    walked = 0
    entries_seen = 0
    directories_seen = 0

    def source_id(relative):
        return identity_prefix + sha((identity_namespace + relative).encode("utf-8", "surrogateescape"))

    if root.is_symlink() or not root.exists():
        return {
            "schema": 1,
            "status": "unavailable",
            "reason": "memory root missing or symlinked",
            "sources": [],
            "cursor": sha((prefix + "root-unavailable").encode()),
        }
    if not root.is_dir():
        return {
            "schema": 1,
            "status": "unavailable",
            "reason": "memory root is not a directory",
            "sources": [],
            "cursor": sha((prefix + "root-invalid").encode()),
        }
    try:
        tree = open_tree(root)
    except OSError:
        return {
            "schema": 1,
            "status": "unavailable",
            "reason": "memory root unavailable",
            "sources": [],
            "cursor": sha((prefix + "root-unavailable").encode()),
        }
    pending = [(tree.root, "", 0)]
    try:
        while pending:
            directory_fd, prefix, depth = pending.pop()
            directories_seen += 1
            if directories_seen > max_entries or depth > max_depth:
                rows.append(
                    {
                        "source_id": source_id(prefix or "."),
                        "source_fingerprint": None,
                        "status": "unavailable",
                        "reason": "directory traversal limit reached",
                    }
                )
                tree.close_directory(directory_fd)
                continue
            try:
                with tree.scan(directory_fd) as scan:
                    entries = sorted(
                        itertools.islice(scan, max_entries - entries_seen + 1),
                        key=lambda entry: entry.name,
                        reverse=True,
                    )
            except OSError:
                rows.append(
                    {
                        "source_id": source_id(prefix or "."),
                        "source_fingerprint": None,
                        "status": "unavailable",
                        "reason": "directory unreadable",
                    }
                )
                tree.close_directory(directory_fd)
                continue
            try:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > max_entries:
                        row = {
                            "source_id": source_id(prefix or "."),
                            "source_fingerprint": None,
                            "status": "unavailable",
                            "reason": "entry inventory limit reached",
                        }
                        rows = rows[: max_entries - 1] + [row]
                        return {
                            "schema": 1,
                            "status": "incomplete",
                            "reason": "bounded directory inventory limit reached",
                            "sources": rows,
                            "cursor": sha(contract.encode(rows)),
                        }
                    relative = (prefix + "/" + entry.name).strip("/")
                    try:
                        info = tree.info(directory_fd, entry)
                    except OSError:
                        rows.append(
                            {
                                "source_id": source_id(relative),
                                "source_fingerprint": None,
                                "status": "unavailable",
                                "reason": "entry unreadable",
                            }
                        )
                        continue
                    if is_link(info):
                        rows.append(
                            {
                                "source_id": source_id(relative),
                                "source_fingerprint": None,
                                "status": "excluded",
                                "reason": "symlink or reparse source excluded",
                            }
                        )
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        try:
                            child_fd = tree.open_directory(directory_fd, entry, info)
                            pending.append((child_fd, relative, depth + 1))
                        except OSError:
                            rows.append(
                                {
                                    "source_id": source_id(relative),
                                    "source_fingerprint": None,
                                    "status": "unavailable",
                                    "reason": "directory changed or unreadable",
                                }
                            )
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        rows.append(
                            {
                                "source_id": source_id(relative),
                                "source_fingerprint": None,
                                "status": "excluded",
                                "reason": "non-regular source excluded",
                            }
                        )
                        continue
                    walked += 1
                    if walked > max_files:
                        return {
                            "schema": 1,
                            "status": "incomplete",
                            "reason": "file inventory limit reached",
                            "sources": rows,
                            "cursor": sha(contract.encode(rows)),
                        }
                    if (
                        info.st_size > max_file_bytes
                        or total + info.st_size > max_total_bytes
                    ):
                        rows.append(
                            {
                                "source_id": source_id(relative),
                                "source_fingerprint": None,
                                "status": "excluded",
                                "reason": "bounded inventory limit exceeded",
                            }
                        )
                        continue
                    try:
                        digest = hashlib.sha256()
                        size = 0
                        with tree.verified_file(directory_fd, entry, info) as source:
                            while True:
                                chunk = source.read(65536)
                                if not chunk:
                                    break
                                size += len(chunk)
                                total += len(chunk)
                                if size > max_file_bytes or total > max_total_bytes:
                                    raise OverflowError()
                                digest.update(chunk)
                        rows.append(
                            {
                                "source_id": source_id(relative),
                                "source_fingerprint": digest.hexdigest(),
                                "status": "not-checked",
                                "reason": "fingerprinted; semantic review required",
                            }
                        )
                    except OverflowError:
                        rows.append(
                            {
                                "source_id": source_id(relative),
                                "source_fingerprint": None,
                                "status": "excluded",
                                "reason": "bounded inventory limit exceeded",
                            }
                        )
                    except OSError:
                        rows.append(
                            {
                                "source_id": source_id(relative),
                                "source_fingerprint": None,
                                "status": "unavailable",
                                "reason": "file changed or unreadable",
                            }
                        )
            finally:
                tree.close_directory(directory_fd)
    finally:
        for directory_fd, _, _ in pending:
            try:
                tree.close_directory(directory_fd)
            except OSError:
                pass
        tree.close()
    rows.sort(key=lambda item: item["source_id"])
    snapshot = sha(contract.encode(rows))
    counts: dict[str, int] = {}
    for item in rows:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "schema": 1,
        "status": "complete" if not counts.get("unavailable") else "incomplete",
        "reason": "metadata inventory only; no import coverage claimed",
        "sources": rows,
        "counts": counts,
        "cursor": snapshot,
    }


def read_tree(root, source_id, *, prefix, identity_namespace=""):
    identity_prefix = prefix
    try:
        tree = open_tree(root)
    except OSError:
        raise CacheBlocked() from None
    pending = [(tree.root, "", 0)]
    entries_seen = 0
    directories_seen = 0
    raw = None
    try:
        while pending and raw is None:
            directory_fd, prefix, depth = pending.pop()
            directories_seen += 1
            if directories_seen > 1024 or depth > 16:
                tree.close_directory(directory_fd)
                raise CacheBlocked()
            try:
                with tree.scan(directory_fd) as scan:
                    entries = sorted(
                        itertools.islice(scan, 1025 - entries_seen),
                        key=lambda e: e.name,
                        reverse=True,
                    )
            except OSError:
                tree.close_directory(directory_fd)
                raise CacheBlocked() from None
            try:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > 1024:
                        raise CacheBlocked()
                    relative = (prefix + "/" + entry.name).strip("/")
                    try:
                        info = tree.info(directory_fd, entry)
                    except OSError:
                        raise CacheBlocked() from None
                    if is_link(info):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        try:
                            child_fd = tree.open_directory(
                                directory_fd, entry, info
                            )
                            pending.append((child_fd, relative, depth + 1))
                        except OSError:
                            raise CacheBlocked() from None
                        continue
                    if stat.S_ISREG(
                        info.st_mode
                    ) and source_id == identity_prefix + sha(
                        (identity_namespace + relative).encode("utf-8", "surrogateescape")
                    ):
                        if info.st_size > 131072:
                            raise CacheBlocked()
                        try:
                            with tree.verified_file(
                                directory_fd, entry, info
                            ) as source:
                                raw = source.read(131073)
                        except OSError:
                            raise CacheBlocked() from None
                        break
            finally:
                tree.close_directory(directory_fd)
        if raw is None:
            raise CacheBlocked()
    except OSError:
        raise CacheBlocked() from None
    finally:
        for directory_fd, _, _ in pending:
            try:
                tree.close_directory(directory_fd)
            except OSError:
                pass
        tree.close()
    return raw
