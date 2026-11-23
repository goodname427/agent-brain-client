"""Reviewed native-source inventory, import, CAS and reconciliation ledgers."""

from agent_brain_client.domain.content import same_record
from agent_brain_client.domain.errors import CacheBlocked
from agent_brain_client.domain.events import native_source_identity
import json
import re
from agent_brain_client.domain import contract
from agent_brain_client.adapters.platforms.filesystem import (
    cache_locked as locked,
    require_platform,
)
from agent_brain_client.adapters.storage.content_limits import (
    APPROVED_NATIVE_ORIGINAL,
    LEGACY_NATIVE_SUMMARY,
    RETAINED_REVIEW_DECISIONS,
    sha,
)


class SourceImports:
    def __init__(self, session, provider):
        self.session = session
        self.provider = provider

    def is_native(self, source_id):
        if source_id.startswith(("codex-memory:", "codebuddy-memory:")) and not source_id.startswith(self.provider.prefix):
            raise CacheBlocked()
        return source_id.startswith(self.provider.prefix)

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
            if self.is_native(source["source_id"]):
                review = reviews["items"].get(key)
                if (
                    not isinstance(review, dict)
                    or review.get("decision") != "shareable"
                    or review.get("reason_code") != LEGACY_NATIVE_SUMMARY
                    or review.get("source_id") != source["source_id"]
                    or review.get("source_fingerprint") != source["source_fingerprint"]
                ):
                    raise CacheBlocked()

    def validate_reconciliation(self, source_keys, kind, identity, record):
        if not source_keys:
            return
        state = self._imports()
        legacy_conflict_keys = None
        for key in source_keys:
            item = state["items"].get(key)
            if (
                item is None
                or item["status"] not in ("conflict", "inventoried")
                or (item["source"]["kind"], item["source"]["id"]) != (kind, identity)
            ):
                raise CacheBlocked()
            if self.is_native(item["source"]["source_id"]):
                # Only evidence from a submitted conflict permits a new resolution.
                if legacy_conflict_keys is None:
                    legacy_conflict_keys = {
                        source_key
                        for row in self.session._queued()
                        if row["state"] in ("conflict", "reconciled")
                        and (row["request"]["params"]["kind"], row["request"]["params"]["id"])
                        == (kind, identity)
                        for source_key in row["reconciles"]["source_keys"]
                    }
                if key not in legacy_conflict_keys:
                    raise CacheBlocked()
            elif item["status"] == "inventoried" and not same_record(
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
        """Stage an explicit plan for non-native sources without file discovery."""
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
                or self.is_native(item["source_id"])
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
                if not isinstance(fingerprint, str) or not re.fullmatch(
                    r"[a-f0-9]{64}", fingerprint
                ):
                    raise CacheBlocked()
        seen = set()
        for item in plan["sources"]:
            self._check_source(item)
            if self.is_native(item["source_id"]) or item["source_id"] in seen:
                raise CacheBlocked()
            seen.add(item["source_id"])
        with locked(self.session.cache / "content.lock"):
            state = self._imports()
            for item in plan["sources"]:
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

    def onboarding_evidence(self):
        """Load retained decisions and CAS evidence without rewriting any ledger."""
        require_platform("cache-storage", self.session.platform)
        with locked(self.session.cache / "content.lock"):
            imports = self._imports()["items"]
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
            submitted = {}
            for row in self.session._queued():
                for key in row["reconciles"]["source_keys"]:
                    confirmed = row["state"] == "confirmed" and self.session._server_saved(
                        row["response"], row["request"],
                    )
                    submitted[key] = confirmed or submitted.get(key, False)
            legacy = {}
            for key, item in imports.items():
                if not self.is_native(item["source"]["source_id"]):
                    continue
                if submitted.get(key):
                    legacy[key] = "legacy-cas-record-retained"
                elif key in submitted or item["request_id"] is not None or item["status"] in (
                    "imported", "duplicate", "pending", "conflict", "reconciled",
                ):
                    legacy[key] = "legacy-receipt-unconfirmed"
            excluded = set()
            for key, review in reviews["items"].items():
                if not isinstance(review, dict) or set(review) != {
                    "source_id", "source_fingerprint", "decision", "reason_code",
                }:
                    raise CacheBlocked()
                source_id = review["source_id"]
                fingerprint = review["source_fingerprint"]
                native_source_identity(source_id, fingerprint)
                if (
                    not self.is_native(source_id)
                    or key != sha(contract.encode([source_id, fingerprint]))
                    or review["reason_code"] not in RETAINED_REVIEW_DECISIONS.get(review["decision"], ())
                ):
                    raise CacheBlocked()
                if review["decision"] != "shareable":
                    excluded.add(source_id)
                elif review["reason_code"] != APPROVED_NATIVE_ORIGINAL:
                    legacy.setdefault(key, "legacy-summary-retained")
            return excluded, legacy

    def read_onboarding_original(self, source_id, fingerprint):
        require_platform("cache-storage", self.session.platform)
        native_source_identity(source_id, fingerprint)
        if not self.is_native(source_id):
            raise CacheBlocked()
        raw, mtime_ns = self.provider.read(source_id, with_mtime=True)
        return self._checked_body(raw, fingerprint), mtime_ns

    @staticmethod
    def _checked_body(raw, fingerprint):
        if len(raw) > 131072 or sha(raw) != fingerprint:
            raise CacheBlocked()
        try:
            return raw.decode("utf-8", "strict")
        except UnicodeDecodeError:
            raise CacheBlocked() from None

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
                        if self.is_native(source["source_id"])
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
            # A retained native plan with no submitted request stays local.
            if self.is_native(source["source_id"]):
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
