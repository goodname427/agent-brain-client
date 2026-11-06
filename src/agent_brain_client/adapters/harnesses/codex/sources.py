"""Codex sources: concrete native behavior, separate from shared use cases."""

from agent_brain_client.domain.content import same_record
from agent_brain_client.domain.errors import CacheBlocked
import itertools
import json
import re
import stat
from agent_brain_client.domain import contract
from agent_brain_client.adapters.platforms.filesystem import (
    cache_locked as locked,
    require_platform,
)
from agent_brain_client.adapters.harnesses.codex.content_limits import (
    REVIEW_DECISIONS,
    sha,
)
from agent_brain_client.adapters.harnesses.codex.memory_inventory import (
    inventory_codex_memories,
)


from agent_brain_client.adapters.platforms.tree import open_tree, is_link


class CodexSourceImports:
    def __init__(self, session):
        self.session = session

    def assert_shareable(self, source_keys):
        if not source_keys:
            return
        imports = self._imports()
        reviews = self.session._load(
            "memory-reviews.json",
            {"schema": 1, "binding": self.session.binding, "items": {}},
        )
        if (
            not isinstance(reviews, dict)
            or reviews.get("binding") != self.session.binding
            or not isinstance(reviews.get("items"), dict)
        ):
            raise CacheBlocked()
        for key in source_keys:
            item = imports["items"].get(key)
            if item is None:
                raise CacheBlocked()
            source = item["source"]
            if source["source_id"].startswith("codex-memory:"):
                review = reviews["items"].get(key)
                if (
                    not isinstance(review, dict)
                    or review.get("decision") != "shareable"
                    or review.get("reason_code")
                    != "safe-cross-project-personal-summary"
                    or review.get("source_id") != source["source_id"]
                    or review.get("source_fingerprint") != source["source_fingerprint"]
                ):
                    raise CacheBlocked()

    def validate_reconciliation(self, source_keys, kind, identity, record):
        if not source_keys:
            return
        state = self._imports()
        for key in source_keys:
            item = state["items"].get(key)
            if (
                item is None
                or item["status"] not in ("conflict", "inventoried")
                or (item["source"]["kind"], item["source"]["id"]) != (kind, identity)
            ):
                raise CacheBlocked()
            if item["status"] == "inventoried" and not same_record(
                kind, item["source"]["record"], record, preserve_enabled=False
            ):
                raise CacheBlocked()

    def confirm_reconciliation(self, source_keys, request_id):
        if not source_keys:
            return
        state = self._imports()
        for key in source_keys:
            item = state["items"][key]
            if item["status"] in ("conflict", "inventoried"):
                item.update(status="reconciled", request_id=request_id)
        self.session._save("imports.json", state)

    def block_reconciliation(self, source_keys, request_id):
        state = self._imports()
        for key in source_keys:
            if key in state["items"] and state["items"][key]["status"] in (
                "inventoried",
                "pending",
            ):
                state["items"][key].update(status="blocked", request_id=request_id)
        self.session._save("imports.json", state)

    def stage_import(self, plan):
        """Explicit reviewed normalized sources only; no native/file discovery."""
        if (
            not isinstance(plan, dict)
            or set(plan) != {"schema", "binding", "sources", "coverage"}
            or plan["schema"] != 1
            or plan["binding"] != self.session.binding
            or not isinstance(plan["sources"], list)
            or len(plan["sources"]) > 256
        ):
            raise CacheBlocked()
        coverage = plan["coverage"]
        if not isinstance(coverage, list) or len(coverage) > 4096:
            raise CacheBlocked()
        for item in coverage:
            if not isinstance(item, dict) or set(item) not in (
                {"source_id", "status", "reason"},
                {"source_id", "source_fingerprint", "status", "reason"},
            ):
                raise CacheBlocked()
            if (
                not isinstance(item["source_id"], str)
                or not 0 < len(item["source_id"]) <= 1024
                or not isinstance(item["status"], str)
                or item["status"]
                not in (
                    "read",
                    "excluded",
                    "unavailable",
                    "not-checked",
                    "duplicate",
                    "skipped",
                    "deferred",
                    "review",
                )
                or not isinstance(item["reason"], str)
                or not 0 < len(item["reason"]) <= 1024
            ):
                raise CacheBlocked()
            if "source_fingerprint" in item:
                fingerprint = item["source_fingerprint"]
                if fingerprint is None:
                    if not item["source_id"].startswith("codex-memory:") or item[
                        "status"
                    ] not in ("excluded", "unavailable"):
                        raise CacheBlocked()
                elif not isinstance(fingerprint, str) or not re.fullmatch(
                    r"[a-f0-9]{64}", fingerprint
                ):
                    raise CacheBlocked()
        seen = set()
        for item in plan["sources"]:
            self._check_source(item)
            if item["source_id"] in seen:
                raise CacheBlocked()
            seen.add(item["source_id"])
        source_fingerprints = {
            item["source_id"]: item["source_fingerprint"] for item in plan["sources"]
        }
        with locked(self.session.cache / "content.lock"):
            state = self._imports()
            memory_inventory = self.session._load("memory-inventory.json")
            reviews = self.session._load(
                "memory-reviews.json",
                {"schema": 1, "binding": self.session.binding, "items": {}},
            )
            if (
                not isinstance(reviews, dict)
                or set(reviews) != {"schema", "binding", "items"}
                or reviews["schema"] != 1
                or reviews["binding"] != self.session.binding
                or not isinstance(reviews["items"], dict)
                or len(reviews["items"]) > 1024
            ):
                raise CacheBlocked()
            for entry in coverage:
                source_id = entry["source_id"]
                if not source_id.startswith("codex-memory:"):
                    continue
                fingerprint = entry.get("source_fingerprint")
                if fingerprint is None:
                    key = sha(contract.encode([source_id, None]))
                else:
                    key = sha(contract.encode([source_id, fingerprint]))
                source = (
                    memory_inventory.get("sources", {}).get(key)
                    if isinstance(memory_inventory, dict)
                    else None
                )
                if (
                    not isinstance(source, dict)
                    or source.get("source_id") != source_id
                    or source.get("source_fingerprint") != fingerprint
                ):
                    raise CacheBlocked()
                review = reviews["items"].get(key)
                imported = state["items"].get(key)
                status = entry["status"]
                if review is not None and (
                    not isinstance(review, dict)
                    or set(review)
                    != {"source_id", "source_fingerprint", "decision", "reason_code"}
                    or review["source_id"] != source_id
                    or review["source_fingerprint"] != fingerprint
                    or review["decision"] not in REVIEW_DECISIONS
                    or review["reason_code"] not in REVIEW_DECISIONS[review["decision"]]
                ):
                    raise CacheBlocked()
                if status == "read":
                    if (
                        fingerprint is None
                        or source.get("status") != "read"
                        and imported is None
                        or not isinstance(review, dict)
                        or review.get("decision") != "shareable"
                        or review.get("reason_code")
                        != "safe-cross-project-personal-summary"
                        or source_fingerprints.get(source_id) != fingerprint
                        and imported is None
                    ):
                        raise CacheBlocked()
                elif status == "excluded":
                    if source.get("status") != "excluded" and not (
                        isinstance(review, dict)
                        and review.get("decision") == "excluded"
                        and review.get("reason_code")
                        == "secret-localonly-or-confidential"
                    ):
                        raise CacheBlocked()
                elif status in ("deferred", "skipped"):
                    if (
                        not isinstance(review, dict)
                        or review.get("decision") != status
                        or review.get("reason_code") not in REVIEW_DECISIONS[status]
                    ):
                        raise CacheBlocked()
                elif status in ("unavailable", "not-checked"):
                    if source.get("status") != status:
                        raise CacheBlocked()
                elif status == "duplicate":
                    if (
                        not isinstance(imported, dict)
                        or imported.get("status") != "duplicate"
                    ):
                        raise CacheBlocked()
                else:
                    raise CacheBlocked()
            for item in plan["sources"]:
                if item["source_id"].startswith("codex-memory:"):
                    if (
                        not isinstance(memory_inventory, dict)
                        or memory_inventory.get("binding") != self.session.binding
                        or not isinstance(memory_inventory.get("sources"), dict)
                    ):
                        raise CacheBlocked()
                    inventory_key = sha(
                        contract.encode([item["source_id"], item["source_fingerprint"]])
                    )
                    source = memory_inventory["sources"].get(inventory_key)
                    imported_key = sha(
                        contract.encode([item["source_id"], item["source_fingerprint"]])
                    )
                    already = state["items"].get(imported_key)
                    if (
                        source is None
                        or source.get("source_id") != item["source_id"]
                        or source.get("source_fingerprint")
                        != item["source_fingerprint"]
                        or (source.get("status") != "read" and already is None)
                    ):
                        raise CacheBlocked()
                    reviews = self.session._load("memory-reviews.json")
                    review = (
                        reviews.get("items", {}).get(imported_key)
                        if isinstance(reviews, dict)
                        and reviews.get("binding") == self.session.binding
                        else None
                    )
                    if (
                        not isinstance(review, dict)
                        or review.get("decision") != "shareable"
                        or review.get("reason_code")
                        != "safe-cross-project-personal-summary"
                        or review.get("source_id") != item["source_id"]
                        or review.get("source_fingerprint")
                        != item["source_fingerprint"]
                    ):
                        raise CacheBlocked()
                key = sha(
                    contract.encode([item["source_id"], item["source_fingerprint"]])
                )
                if key in state["items"]:
                    if state["items"][key]["source"] != item:
                        raise CacheBlocked()
                    continue
                state["items"][key] = {
                    "source": item,
                    "status": "inventoried",
                    "request_id": None,
                    "remote": None,
                }
            if len(state["items"]) > 1024:
                raise CacheBlocked()
            merged = {
                (entry["source_id"], entry.get("source_fingerprint")): entry
                for entry in state["coverage"]
            }
            merged.update(
                {
                    (entry["source_id"], entry.get("source_fingerprint")): entry
                    for entry in coverage
                }
            )
            if len(merged) > 4096:
                raise CacheBlocked()
            state["coverage"] = sorted(
                merged.values(),
                key=lambda value: (
                    value["source_id"],
                    value.get("source_fingerprint", ""),
                ),
            )
            self.session._save("imports.json", json.loads(contract.encode(state)))
        return {
            "status": "content-sources-inventoried",
            "sources": len(plan["sources"]),
            "data_transferred": False,
            "native_changed": False,
        }

    def inventory_memory_sources(self):
        """Persist a path-free memory inventory in the existing private cache.

        This inventories only; it never creates an import plan or contacts the
        Server. Unchanged fingerprints retain their review/import state.
        """
        require_platform("cache-storage", self.session.platform)
        with locked(self.session.cache / "content.lock"):
            fresh = inventory_codex_memories(self.session.native_memory_home)
            previous = self.session._load("memory-inventory.json", None)
            if previous is not None and (
                not isinstance(previous, dict)
                or set(previous) != {"schema", "binding", "cursor", "status", "sources"}
                or previous["schema"] != 1
                or previous["binding"] != self.session.binding
                or not isinstance(previous["sources"], dict)
                or len(previous["sources"]) > 1024
            ):
                raise CacheBlocked()
            old = previous["sources"] if previous else {}
            tracked = self._imports()
            active_keys = {
                key
                for key, item in tracked["items"].items()
                if item["status"] in ("pending", "conflict")
            }
            active_keys.update(
                key
                for row in self.session._queued()
                if row["state"] in ("pending", "conflict")
                for key in row["reconciles"]["source_keys"]
            )
            current = {}
            seen_ids = set()
            for row in fresh["sources"]:
                source_id = row["source_id"]
                fingerprint = row["source_fingerprint"]
                seen_ids.add(source_id)
                inventory_key = sha(contract.encode([source_id, fingerprint]))
                prior = old.get(inventory_key)
                if prior and prior.get("source_fingerprint") == fingerprint:
                    row = dict(row, status=prior["status"], reason=prior["reason"])
                current[inventory_key] = row
            for inventory_key, prior in old.items():
                if inventory_key in current:
                    continue
                if inventory_key in active_keys:
                    current[inventory_key] = prior
                elif (
                    fresh["status"] != "complete" and prior["source_id"] not in seen_ids
                ):
                    current[inventory_key] = prior
            if len(current) > 1024:
                raise CacheBlocked()
            fresh = dict(
                fresh,
                sources=sorted(current.values(), key=lambda item: item["source_id"]),
            )
            counts: dict[str, int] = {}
            for item in fresh["sources"]:
                counts[item["status"]] = counts.get(item["status"], 0) + 1
            fresh["counts"] = counts
            fresh["cursor"] = sha(contract.encode(fresh["sources"]))
            ledger = {
                "schema": 1,
                "binding": self.session.binding,
                "cursor": fresh["cursor"],
                "status": fresh["status"],
                "sources": current,
            }
            self.session._save("memory-inventory.json", ledger)
            sources = []
            review_ledger = self.session._load(
                "memory-reviews.json",
                {"schema": 1, "binding": self.session.binding, "items": {}},
            )
            if (
                not isinstance(review_ledger, dict)
                or set(review_ledger) != {"schema", "binding", "items"}
                or review_ledger["schema"] != 1
                or review_ledger["binding"] != self.session.binding
                or not isinstance(review_ledger["items"], dict)
                or len(review_ledger["items"]) > 1024
            ):
                raise CacheBlocked()
            live_review_keys = set(current) | active_keys
            review_ledger["items"] = {
                key: value
                for key, value in review_ledger["items"].items()
                if key in live_review_keys
            }
            self.session._save("memory-reviews.json", review_ledger)
            for source_id, item in current.items():
                value = dict(item)
                fingerprint = value["source_fingerprint"]
                if isinstance(fingerprint, str) and value["status"] not in (
                    "excluded",
                    "unavailable",
                ):
                    imported = tracked["items"].get(
                        sha(contract.encode([value["source_id"], fingerprint]))
                    )
                    if imported is not None:
                        value.update(
                            status=imported["status"],
                            reason="linked to reviewed import ledger",
                        )
                    review = review_ledger["items"].get(
                        sha(contract.encode([value["source_id"], fingerprint]))
                    )
                    if review is not None:
                        if (
                            not isinstance(review, dict)
                            or set(review)
                            != {
                                "source_id",
                                "source_fingerprint",
                                "decision",
                                "reason_code",
                            }
                            or review["source_id"] != value["source_id"]
                            or review["source_fingerprint"] != fingerprint
                            or review["decision"] not in REVIEW_DECISIONS
                            or review["reason_code"]
                            not in REVIEW_DECISIONS[review["decision"]]
                        ):
                            raise CacheBlocked()
                        value.update(
                            review_decision=review["decision"],
                            review_reason_code=review["reason_code"],
                        )
                sources.append(value)
            sources.sort(
                key=lambda item: (item["source_id"], item["source_fingerprint"] or "")
            )
            cursor = sha(contract.encode(sources))
            counts = {}
            for item in sources:
                counts[item["status"]] = counts.get(item["status"], 0) + 1
            return {
                "status": fresh["status"],
                "counts": counts,
                "cursor": cursor,
                "sources": sources,
                "reason": fresh.get(
                    "reason", "metadata inventory only; no import coverage claimed"
                ),
                "data_transferred": False,
                "account_complete": False,
            }

    def read_memory_source(self, source_id, fingerprint):
        """Read one explicitly selected, inventoried Codex Memory file.

        The text is returned only to the local MCP caller and is never written
        into the cache or sent to AgentBrain. A bounded no-follow open and a
        post-read digest bind it to the inventory row reviewed by the caller.
        """
        if not isinstance(source_id, str) or not re.fullmatch(
            r"codex-memory:[a-f0-9]{64}", source_id
        ):
            raise CacheBlocked()
        if not isinstance(fingerprint, str) or not re.fullmatch(
            r"[a-f0-9]{64}", fingerprint
        ):
            raise CacheBlocked()
        require_platform("cache-storage", self.session.platform)
        with locked(self.session.cache / "content.lock"):
            inventory = self.session._load("memory-inventory.json")
            if (
                not isinstance(inventory, dict)
                or set(inventory)
                != {"schema", "binding", "cursor", "status", "sources"}
                or inventory["schema"] != 1
                or inventory["binding"] != self.session.binding
                or not isinstance(inventory["sources"], dict)
            ):
                raise CacheBlocked()
            key = sha(contract.encode([source_id, fingerprint]))
            row = inventory["sources"].get(key)
            if (
                row is None
                or row.get("source_id") != source_id
                or row.get("source_fingerprint") != fingerprint
                or row.get("status") not in ("not-checked", "read")
            ):
                raise CacheBlocked()
            root = self.session.native_memory_home / "memories"
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
                            ) and source_id == "codex-memory:" + sha(
                                relative.encode("utf-8", "surrogateescape")
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
            if len(raw) > 131072 or sha(raw) != fingerprint:
                raise CacheBlocked()
            try:
                body = raw.decode("utf-8", "strict")
            except UnicodeDecodeError:
                raise CacheBlocked() from None
            row = dict(
                row,
                status="read",
                reason="selected body read locally; review and normalize before staging",
            )
            inventory["sources"][key] = row
            self.session._save("memory-inventory.json", inventory)
            return {
                "source_id": source_id,
                "source_fingerprint": fingerprint,
                "text": body,
                "data_transferred": False,
                "native_changed": False,
                "note": "Local-only bounded read. Review for sensitive or cross-project details before preparing an import.",
            }

    def review_memory_source(self, source_id, fingerprint, decision, reason_code):
        """Persist an explicit content review decision without retaining source text."""
        decisions = REVIEW_DECISIONS
        if (
            not isinstance(source_id, str)
            or not re.fullmatch(r"codex-memory:[a-f0-9]{64}", source_id)
            or not isinstance(fingerprint, str)
            or not re.fullmatch(r"[a-f0-9]{64}", fingerprint)
            or decision not in decisions
            or reason_code not in decisions[decision]
        ):
            raise CacheBlocked()
        require_platform("cache-storage", self.session.platform)
        with locked(self.session.cache / "content.lock"):
            inventory = self.session._load("memory-inventory.json")
            key = sha(contract.encode([source_id, fingerprint]))
            row = (
                inventory.get("sources", {}).get(key)
                if isinstance(inventory, dict)
                else None
            )
            if (
                row is None
                or row.get("source_id") != source_id
                or row.get("source_fingerprint") != fingerprint
                or row.get("status") != "read"
            ):
                raise CacheBlocked()
            state = self.session._load(
                "memory-reviews.json",
                {"schema": 1, "binding": self.session.binding, "items": {}},
            )
            if (
                not isinstance(state, dict)
                or set(state) != {"schema", "binding", "items"}
                or state["schema"] != 1
                or state["binding"] != self.session.binding
                or not isinstance(state["items"], dict)
                or len(state["items"]) > 1024
            ):
                raise CacheBlocked()
            state["items"][key] = {
                "source_id": source_id,
                "source_fingerprint": fingerprint,
                "decision": decision,
                "reason_code": reason_code,
            }
            if len(state["items"]) > 1024:
                raise CacheBlocked()
            self.session._save("memory-reviews.json", state)
            imports = self._imports()
            item = imports["items"].get(key)
            if (
                decision == "shareable"
                and item is not None
                and item["status"] == "blocked"
            ):
                item.update(status="inventoried", request_id=None, remote=None)
                self.session._save("imports.json", imports)
            if decision != "shareable":
                if item is not None and item["status"] in ("inventoried", "pending"):
                    item.update(
                        status="blocked",
                        request_id=None
                        if item["status"] == "inventoried"
                        else item["request_id"],
                    )
                    self.session._save("imports.json", imports)
                for queued in self.session._queued():
                    if (
                        queued["state"] == "pending"
                        and key in queued["reconciles"]["source_keys"]
                    ):
                        queued.update(
                            state="blocked",
                            response={
                                "request_id": queued["request"]["request_id"],
                                "error": {
                                    "code": "native_source_review_revoked",
                                    "retryable": False,
                                },
                                "persistence": {"durable": False},
                            },
                        )
                        self.session._save(
                            "outbox/" + queued["request"]["request_id"] + ".json",
                            queued,
                        )
        return {
            "status": "content-source-reviewed",
            "source_id": source_id,
            "decision": decision,
            "reason_code": reason_code,
            "text_persisted": False,
            "data_transferred": False,
        }

    def stage_memory_import(self, source_id, fingerprint, kind, identity, record):
        """Stage a normalized Memory candidate only after its exact source was read."""
        require_platform("cache-storage", self.session.platform)
        with locked(self.session.cache / "content.lock"):
            inventory = self.session._load("memory-inventory.json")
            if (
                not isinstance(inventory, dict)
                or inventory.get("binding") != self.session.binding
                or not isinstance(inventory.get("sources"), dict)
            ):
                raise CacheBlocked()
            key = sha(contract.encode([source_id, fingerprint]))
            row = inventory["sources"].get(key)
            if (
                row is None
                or row.get("status") != "read"
                or row.get("source_id") != source_id
                or row.get("source_fingerprint") != fingerprint
            ):
                raise CacheBlocked()
            reviews = self.session._load("memory-reviews.json")
            review = (
                reviews.get("items", {}).get(key)
                if isinstance(reviews, dict)
                and reviews.get("binding") == self.session.binding
                else None
            )
            if (
                not isinstance(review, dict)
                or review.get("decision") != "shareable"
                or review.get("reason_code") != "safe-cross-project-personal-summary"
                or review.get("source_id") != source_id
                or review.get("source_fingerprint") != fingerprint
            ):
                raise CacheBlocked()
            self._check_source(
                {
                    "source_id": source_id,
                    "source_fingerprint": fingerprint,
                    "kind": kind,
                    "id": identity,
                    "record": record,
                }
            )
        plan = {
            "schema": 1,
            "binding": self.session.binding,
            "sources": [
                {
                    "source_id": source_id,
                    "source_fingerprint": fingerprint,
                    "kind": kind,
                    "id": identity,
                    "record": record,
                }
            ],
            "coverage": [
                {
                    "source_id": source_id,
                    "source_fingerprint": fingerprint,
                    "status": "read",
                    "reason": "selected source reviewed and normalized for import",
                }
            ],
        }
        result = self.stage_import(plan)
        with locked(self.session.cache / "content.lock"):
            inventory = self.session._load("memory-inventory.json")
            inventory_key = sha(contract.encode([source_id, fingerprint]))
            row = dict(
                inventory["sources"][inventory_key],
                reason="reviewed normalized candidate staged",
            )
            inventory["sources"][inventory_key] = row
            self.session._save("memory-inventory.json", inventory)
        return dict(
            result,
            status="content-source-staged",
            source_id=source_id,
            source_key=sha(contract.encode([source_id, fingerprint])),
        )

    def _check_source(self, item):
        if (
            not isinstance(item, dict)
            or set(item) != {"source_id", "source_fingerprint", "kind", "id", "record"}
            or not isinstance(item["source_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", item["source_id"])
            or not isinstance(item["source_fingerprint"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", item["source_fingerprint"])
        ):
            raise CacheBlocked()
        contract.validate(
            "content_put",
            {
                "kind": item["kind"],
                "id": item["id"],
                "record": item["record"],
                "expected_revision": "missing",
            },
        )
        if (
            item["kind"] not in self.session.policy["kinds"]
            or not self.session._allows(item["kind"], item["id"])
            or item["kind"] in ("rule", "skill")
            and item["record"]["enabled"]
        ):
            raise CacheBlocked()

    def removal_blocker(self):
        """Inventory retained sources without transport identity or recovery.

        Removal must not activate a credential to derive queue_sha. Preserve its
        existing metadata-only checks; normal imports use the stronger binding
        and content validation in _imports(). Neither path changes the ledger.
        """
        state = self.session._load("imports.json")
        if state is None:
            return None
        if (
            not isinstance(state, dict)
            or set(state) != {"schema", "policy_sha", "items", "coverage"}
            or state["schema"] != 1
            or not isinstance(state["items"], dict)
        ):
            return "Import inventory retained"
        for item in state["items"].values():
            phase = item.get("status") if isinstance(item, dict) else None
            if phase not in (
                "inventoried",
                "pending",
                "imported",
                "duplicate",
                "conflict",
                "blocked",
                "reconciled",
            ):
                return "Unknown import retained"
            if phase in ("inventoried", "pending", "conflict", "blocked"):
                return "Unfinished import retained; recover using the existing MCP"
        return None

    def _imports(self):
        state = self.session._load(
            "imports.json",
            {
                "schema": 1,
                "policy_sha": self.session.queue_sha,
                "items": {},
                "coverage": [],
            },
        )
        if (
            not isinstance(state, dict)
            or set(state) != {"schema", "policy_sha", "items", "coverage"}
            or state["schema"] != 1
            or state["policy_sha"] != self.session.queue_sha
            or not isinstance(state["items"], dict)
            or len(state["items"]) > 1024
        ):
            raise CacheBlocked()
        for key, item in state["items"].items():
            if (
                not isinstance(item, dict)
                or set(item) != {"source", "status", "request_id", "remote"}
                or item["status"]
                not in (
                    "inventoried",
                    "pending",
                    "imported",
                    "duplicate",
                    "conflict",
                    "blocked",
                    "reconciled",
                )
            ):
                raise CacheBlocked()
            self._check_source(item["source"])
            if key != sha(
                contract.encode(
                    [item["source"]["source_id"], item["source"]["source_fingerprint"]]
                )
            ):
                raise CacheBlocked()
            if item["request_id"] is not None and not re.fullmatch(
                r"[A-Za-z0-9_-]{16,128}", item["request_id"]
            ):
                raise CacheBlocked()
        return state

    def reconcile_pending(self):
        state = self._imports()
        linked_writes = {}
        for queued in self.session._queued():
            if queued["state"] not in ("pending", "confirmed", "conflict"):
                continue
            for source_key in queued["reconciles"]["source_keys"]:
                linked_writes[source_key] = queued
        for key, item in state["items"].items():
            source = item["source"]
            kind, identity = source["kind"], source["id"]
            if item["status"] == "reconciled":
                continue
            if item["request_id"]:
                row = self.session._load("outbox/" + item["request_id"] + ".json")
                if row is None:
                    raise CacheBlocked()
                if row["state"] == "confirmed":
                    item["status"] = (
                        "reconciled"
                        if source["source_id"].startswith("codex-memory:")
                        else "imported"
                    )
                elif row["state"] == "reconciled":
                    # A resolver that acknowledges only the old request ID does
                    # not prove that this source was included in the merged
                    # record. Promote the source only when the confirmed
                    # resolution explicitly names its source key.
                    resolution_id = row["resolution_request_id"]
                    resolution = (
                        self.session._load("outbox/" + resolution_id + ".json")
                        if resolution_id
                        else None
                    )
                    if (
                        resolution is not None
                        and resolution["state"] == "confirmed"
                        and key in resolution["reconciles"]["source_keys"]
                    ):
                        item.update(status="reconciled", request_id=resolution_id)
                    else:
                        continue
                else:
                    item["status"] = row["state"]
                if (
                    row["state"] in ("conflict", "blocked")
                    and row["remote"] is not None
                ):
                    item["remote"] = row["remote"]
                continue
            if item["status"] != "inventoried":
                continue
            linked = linked_writes.get(key)
            if linked is not None:
                if linked["state"] == "confirmed":
                    item.update(
                        status="reconciled", request_id=linked["request"]["request_id"]
                    )
                elif linked["state"] in ("pending", "conflict", "blocked"):
                    item.update(
                        status=linked["state"],
                        request_id=linked["request"]["request_id"],
                        remote=linked["remote"],
                    )
                # An explicit source-linked CAS owns this item. Reflect its
                # lifecycle before deciding whether an automatic import applies.
                continue
            # Native Memory staging is local review state; only a caller's explicit
            # content_put carrying this source_key may cross the Server boundary.
            if source["source_id"].startswith("codex-memory:"):
                continue
            try:
                remote = self.session._read_target(kind, identity)
            except Exception:
                break
            current = remote["record"]
            desired = source["record"]
            if same_record(kind, desired, current):
                item.update(status="duplicate", remote=remote)
                continue
            if current is None and remote["revision"] != "missing":
                item.update(status="conflict", remote=remote)
                continue
            if current is not None and kind == "memory":
                # Onboarding preserves occupied topics; a changed source is not
                # authority to replace their unique facts, even from an old base.
                item.update(status="conflict", remote=remote)
                continue
            if current is not None:
                previous = [
                    v["source"]["record"]
                    for k, v in state["items"].items()
                    if k != key
                    and v["source"]["source_id"] == source["source_id"]
                    and (v["source"]["kind"], v["source"]["id"]) == (kind, identity)
                    and v["status"] in ("imported", "duplicate")
                ]
                if not any(same_record(kind, old, current) for old in previous):
                    item.update(status="conflict", remote=remote)
                    continue
                desired = dict(desired, enabled=current["enabled"])
            params = {
                "kind": kind,
                "id": identity,
                "record": desired,
                "expected_revision": remote["revision"],
            }
            rid = "import_" + sha(contract.encode([key, params]))
            self.session._enqueue(
                {"request_id": rid, "method": "content_put", "params": params}
            )
            item.update(status="pending", request_id=rid, remote=remote)
            self.session._save("imports.json", state)
        if state["items"]:
            self.session._save("imports.json", state)

    def import_status(self):
        state = self._imports()
        counts: dict[str, int] = {}
        for item in state["items"].values():
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        conflicts = [
            {
                "source_key": key,
                "source_id": item["source"]["source_id"],
                "kind": item["source"]["kind"],
                "id": item["source"]["id"],
                "remote_revision": (item["remote"] or {}).get("revision"),
            }
            for key, item in state["items"].items()
            if item["status"] == "conflict"
        ]
        source_inventory = self.session._load("memory-inventory.json")
        inventory_summary = None
        if source_inventory is not None:
            if (
                not isinstance(source_inventory, dict)
                or set(source_inventory)
                != {"schema", "binding", "cursor", "status", "sources"}
                or source_inventory["schema"] != 1
                or source_inventory["binding"] != self.session.binding
                or not isinstance(source_inventory["sources"], dict)
                or len(source_inventory["sources"]) > 1024
            ):
                raise CacheBlocked()
            inventory_counts: dict[str, int] = {}
            for inventory_key, item in source_inventory["sources"].items():
                if (
                    not isinstance(item, dict)
                    or set(item)
                    != {"source_id", "source_fingerprint", "status", "reason"}
                    or item["status"]
                    not in (
                        "not-checked",
                        "read",
                        "inventoried",
                        "imported",
                        "duplicate",
                        "conflict",
                        "blocked",
                        "reconciled",
                        "excluded",
                        "unavailable",
                    )
                ):
                    raise CacheBlocked()
                status = item["status"]
                fingerprint = item["source_fingerprint"]
                if isinstance(fingerprint, str) and item["status"] not in (
                    "excluded",
                    "unavailable",
                ):
                    tracked = state["items"].get(
                        sha(contract.encode([item["source_id"], fingerprint]))
                    )
                    if tracked is not None:
                        status = tracked["status"]
                inventory_counts[status] = inventory_counts.get(status, 0) + 1
            inventory_summary = {
                "status": source_inventory["status"],
                "counts": inventory_counts,
                "cursor": source_inventory["cursor"],
                "account_complete": False,
            }
        return {
            "counts": counts,
            "conflicts": conflicts,
            "coverage": state["coverage"],
            "account_complete": False,
            "inventoried_sources": len(state["items"]),
            "source_inventory": inventory_summary,
            "scope": "explicit-reviewed-plan-only",
            "coverage_note": "Duplicate is covered; inaccessible/not-checked sources remain gaps. Three-topic retrieval is not an import quota.",
        }
