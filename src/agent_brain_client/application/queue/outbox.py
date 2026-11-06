"""Shared content outbox and normalized turn submission, independent of Harness I/O.

Hosts supply storage, locking, target/source validation and native mapping. No
host config, transcript discovery, credentials or context injection lives here.
"""

from agent_brain_client.domain.content import same_record
from agent_brain_client.domain.errors import CacheBlocked
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable
from agent_brain_client.application.queue.ports import SourceReconciliation

import agent_brain_client.domain.contract as contract
from agent_brain_client.application.control import check_request


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class DurableOutbox:
    """Existing schema-1 CAS outbox; host-specific methods are supplied by subclasses."""

    cache: Path
    platform: str
    policy: dict[str, Any]
    queue_sha: str
    sources: SourceReconciliation
    client: Any
    methods: Callable[..., Any]
    _allows: Callable[..., Any]
    _safe: Callable[..., Any]
    _load: Callable[..., Any]
    _save: Callable[..., Any]
    _validate_target: Callable[..., Any]
    _read_target: Callable[..., Any]
    _outbox_lock: Callable[..., Any]
    _outbox_stage: Callable[..., Any]
    _outbox_locked: Callable[..., Any]
    _require_storage: Callable[..., Any]
    _queued: Callable[..., Any]

    def submit_rule_capture(self, request: dict, *, gate: Callable[[], bool]) -> dict:
        """Stage and retry one explicit Rule observation under this queue's lock.

        The queue owns its ledger and cache lock; the memory use case supplies
        only immutable request bytes and a live coverage/source predicate.
        """
        if (
            request.get("method") != "content_put"
            or request.get("params", {}).get("kind") != "memory"
            or not request.get("request_id", "").startswith("agentbrain_rule_")
        ):
            raise CacheBlocked()
        self._enqueue(request)
        with self._outbox_locked(self.cache / "content.lock"):
            self._drain(
                request["request_id"],
                event_gate=gate,
                exact_request_id=request["request_id"],
            )
            row = self._load("outbox/" + request["request_id"] + ".json")
            if row is None:
                raise CacheBlocked()
            return self._queue_result(row)

    def invoke_write(self, request, reconciles=None, *, event_gate=None):
        """One write/replay implementation for native mapping and event hosts."""
        check_request()
        self._require_storage()
        method = request.get("method")
        with (
            self._outbox_stage("content-write"),
            self._outbox_lock(self.cache / "content.lock", "content-lock"),
        ):
            row = self._enqueue(
                request, reconciles
            )  # Precedes every transport/mapping operation.
            if not self._event_allowed(request, event_gate):
                return dict(
                    self._queue_result(row),
                    local_refresh={
                        "status": "event-disabled",
                        "native_changed": False,
                        "model_loaded": "unknown",
                    },
                )
            if row["state"] == "blocked":
                # Only an explicit identical call retries a blocked request.
                # Startup/refresh never discard or automatically replay it.
                try:
                    response = (
                        self.client.request(request)
                        if self._event_allowed(request, event_gate)
                        else None
                    )
                except Exception:
                    response = None
                if self._server_saved(response, request):
                    row.update(state="confirmed", response=response)
                    self._save("outbox/" + request["request_id"] + ".json", row)
                elif (
                    isinstance(response, dict)
                    and response.get("request_id") == request["request_id"]
                    and isinstance(response.get("error"), dict)
                    and response["error"].get("code") == "revision_conflict"
                ):
                    row.update(state="conflict", response=response)
                    try:
                        row["remote"] = self._read_target(
                            request["params"]["kind"], request["params"]["id"]
                        )
                    except Exception:
                        pass
                    self._save("outbox/" + request["request_id"] + ".json", row)
            # Only an explicit same-ID retry upgrades an old local receipt.
            # No automatic loop over the backed-up journal or new scheduler.
            elif (
                row["state"] == "confirmed"
                and (row["response"] or {}).get("persistence", {}).get("state")
                == "local-confirmed"
            ):
                try:
                    response = self.client.request(request)
                    if self._server_saved(response, request):
                        row["response"] = response
                        self._save("outbox/" + request["request_id"] + ".json", row)
                except Exception:
                    pass
            with self._outbox_stage("outbox-drain"):
                self._drain(
                    request["request_id"] if method == "memory_event_submit" else None,
                    event_gate=event_gate,
                )
            with self._outbox_stage("outbox-result"):
                result = self._queue_result(
                    self._load("outbox/" + request["request_id"] + ".json")
                )
        return result

    def _check_write(self, request):
        if not isinstance(request, dict) or set(request) != {
            "request_id",
            "method",
            "params",
        }:
            raise CacheBlocked()
        rid = request["request_id"]
        method = request["method"]
        params = request["params"]
        if (
            not isinstance(rid, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", rid)
            or method not in contract.WRITES
            or method not in self.methods()
        ):
            raise CacheBlocked()
        contract.validate(method, params)
        if params["kind"] not in self.policy["kinds"] or not self._allows(
            params["kind"], params["id"]
        ):
            raise CacheBlocked()

    def _enqueue(self, request, reconciles=None):
        # Serialize atomic appends separately from the content transaction,
        # which may wait on remote I/O. Native events can become durable pending
        # rows before that wait; existing rows are only checked, never rewritten.
        path = self.cache / "outbox-enqueue.lock"
        self._safe(path, self.cache)
        with self._outbox_lock(path, "outbox-lock"):
            with self._outbox_stage("outbox-enqueue"):
                return self._enqueue_locked(request, reconciles)

    def prepare_memory_write(self, request, context):
        """Freeze provenance once, including retries made after a host restart."""
        if "source_context" in request["params"]:
            return request
        with self._outbox_lock(self.cache / "content.lock", "content-lock"):
            old = self._load("outbox/" + request["request_id"] + ".json")
        if old is not None:
            context = old["request"]["params"].get("source_context")
            if context is None:
                return request  # Exact legacy replay stays exact.
        return dict(request, params=dict(request["params"], source_context=context))

    def _enqueue_locked(self, request, reconciles=None):
        self._check_write(request)
        if reconciles is None:
            reconciles = {"request_ids": [], "source_keys": []}
        if not isinstance(reconciles, dict) or set(reconciles) != {
            "request_ids",
            "source_keys",
        }:
            raise CacheBlocked()
        for field, pattern in [
            ("request_ids", r"[A-Za-z0-9_-]{16,128}"),
            ("source_keys", r"[a-f0-9]{64}"),
        ]:
            values = reconciles[field]
            if (
                not isinstance(values, list)
                or len(values) > 256
                or any(
                    not isinstance(v, str) or not re.fullmatch(pattern, v)
                    for v in values
                )
                or len(set(values)) != len(values)
            ):
                raise CacheBlocked()
        # A prior idempotent outbox entry must not bypass a later review revocation.
        if request["method"] == "content_set_enabled" and reconciles["source_keys"]:
            raise CacheBlocked()
        self.sources.assert_shareable(reconciles["source_keys"])
        request = json.loads(contract.encode(request))
        digest = sha(contract.encode(request))
        name = "outbox/" + request["request_id"] + ".json"
        old = self._load(name)
        if old is not None:
            if (
                old["request_sha"] != digest
                or old["request"] != request
                or old["reconciles"] != reconciles
            ):
                raise CacheBlocked()
            self._queued()
            return old
        rows = self._queued()
        if len(rows) >= 1024:
            raise CacheBlocked()
        target = (request["params"]["kind"], request["params"]["id"])
        for rid in reconciles["request_ids"]:
            previous = next(
                (r for r in rows if r["request"]["request_id"] == rid), None
            )
            if (
                previous is None
                or previous["state"] != "conflict"
                or (
                    previous["request"]["params"]["kind"],
                    previous["request"]["params"]["id"],
                )
                != target
            ):
                raise CacheBlocked()
            if (
                request["method"] == "content_set_enabled"
                and previous["request"]["method"] != "content_set_enabled"
            ):
                raise CacheBlocked()
        self.sources.validate_reconciliation(
            reconciles["source_keys"],
            target[0],
            target[1],
            request["params"].get("record"),
        )
        row = {
            "schema": 1,
            "policy_sha": self.queue_sha,
            "created": time.time_ns(),
            "request": request,
            "request_sha": digest,
            "state": "pending",
            "response": None,
            "remote": None,
            "reconciles": reconciles,
            "resolution_request_id": None,
        }
        self._save(name, row)
        return row

    def _finish_reconciliation(self, row):
        if row["state"] != "confirmed":
            return False
        resolved = False
        for rid in row["reconciles"]["request_ids"]:
            previous = self._load("outbox/" + rid + ".json")
            if previous["state"] == "conflict":
                previous.update(
                    state="reconciled",
                    resolution_request_id=row["request"]["request_id"],
                )
                self._save("outbox/" + rid + ".json", previous)
                resolved = True
        self.sources.confirm_reconciliation(
            row["reconciles"]["source_keys"], row["request"]["request_id"]
        )
        return resolved

    @staticmethod
    def _event_allowed(request, event_gate):
        rule = request["request_id"].startswith("agentbrain_rule_")
        if request["method"] != "memory_event_submit" and not rule:
            return True
        if event_gate is None:
            return not rule
        try:
            return event_gate() is True
        except Exception:
            return False

    def _drain(self, event_request_id=None, event_gate=None, *, exact_request_id=None):
        # A resolver can follow older waiters in creation order. Revisit those
        # only after confirmed reconciliation made progress. An uncertain send
        # ends the whole drain, so this never loops over an unconfirmed request.
        while self._drain_pass(
            event_request_id, event_gate, exact_request_id=exact_request_id
        ):
            pass

    def _drain_pass(
        self, event_request_id=None, event_gate=None, *, exact_request_id=None
    ):
        resolved = False
        for row in self._queued():
            request = row["request"]
            params = request["params"]
            target = (params["kind"], params["id"])
            if (
                exact_request_id is not None
                and request["request_id"] != exact_request_id
            ):
                continue
            # Captured native/Rule text requires an explicit retry and current
            # source/route policy; ordinary refresh never replays it.
            rule = request["request_id"].startswith("agentbrain_rule_")
            if (request["method"] == "memory_event_submit" or rule) and request[
                "request_id"
            ] != event_request_id:
                continue
            if rule and not self._event_allowed(request, event_gate):
                continue
            if row["state"] in ("conflict", "blocked"):
                if row["state"] == "conflict" and row["remote"] is None:
                    try:
                        row["remote"] = self._read_target(*target)
                        self._save("outbox/" + request["request_id"] + ".json", row)
                    except Exception:
                        pass
                # A terminal refusal has no unknown remote outcome. Retain it
                # for review, but let a fresh CAS write proceed independently.
                continue
            if row["state"] == "confirmed":
                resolved = self._finish_reconciliation(row) or resolved
                continue
            if row["state"] != "pending":
                continue
            try:
                self.sources.assert_shareable(row["reconciles"]["source_keys"])
            except CacheBlocked:
                row.update(
                    state="blocked",
                    response={
                        "request_id": request["request_id"],
                        "error": {
                            "code": "native_source_review_revoked",
                            "retryable": False,
                        },
                        "persistence": {"durable": False},
                    },
                )
                self._save("outbox/" + request["request_id"] + ".json", row)
                self.sources.block_reconciliation(
                    row["reconciles"]["source_keys"], request["request_id"]
                )
                continue
            if not self._event_allowed(request, event_gate):
                return False
            try:
                response = self.client.request(json.loads(contract.encode(request)))
            except Exception:
                return False
            if (
                not isinstance(response, dict)
                or response.get("request_id") != request["request_id"]
            ):
                return False
            if "error" not in response:
                if not self._server_saved(response, request):
                    return False
                row.update(state="confirmed", response=response)
            else:
                error = response["error"]
                if not isinstance(error, dict):
                    return False
                if error.get("code") == "revision_conflict":
                    row.update(state="conflict", response=response)
                    try:
                        row["remote"] = self._read_target(*target)
                    except Exception:
                        pass
                elif error.get("retryable") is False and error.get("code") not in (
                    "server_proxy_unconfirmed",
                    "ssh_proxy_unconfirmed",
                    "persistence_pending",
                    "pending_write_must_retry",
                ):
                    row.update(state="blocked", response=response)
                else:
                    return False
            with self._outbox_stage(
                "outbox-confirmation"
                if row["state"] == "confirmed"
                else "outbox-response"
            ):
                self._save("outbox/" + request["request_id"] + ".json", row)
            with self._outbox_stage("outbox-reconciliation"):
                resolved = self._finish_reconciliation(row) or resolved
        return resolved

    def queue_status(self):
        rows = self._queued()
        return {
            state: sum(row["state"] == state for row in rows)
            for state in ("pending", "confirmed", "conflict", "blocked", "reconciled")
        }

    def inspect_queue(self, *, request_id=None, cursor=None):
        """Read one locked snapshot; never fetch, drain, repair or map content."""
        check_request()
        with (
            self._outbox_stage("queue-inspection"),
            self._outbox_locked(
                self.cache / "content.lock", cancel_check=check_request
            ),
        ):
            try:
                rows = self._queued()
            except (ValueError, TypeError, KeyError):
                raise CacheBlocked() from None
            check_request()
            counts = {
                state: sum(row["state"] == state for row in rows)
                for state in (
                    "pending",
                    "confirmed",
                    "conflict",
                    "blocked",
                    "reconciled",
                )
            }
            base = {
                "counts": counts,
                "data_transferred": False,
                "native_changed": False,
                "remote_snapshot_current": False,
            }
            if request_id is not None:
                row = next(
                    (row for row in rows if row["request"]["request_id"] == request_id),
                    None,
                )
                if row is None:
                    return dict(base, status="not-found", request_id=request_id)
                # Only this exact read exposes bodies; listing contains metadata.
                return dict(
                    base,
                    status="retained",
                    request=row["request"],
                    reconciles=row["reconciles"],
                    response=row["response"],
                    remote_snapshot=row["remote"],
                    local_queue=self._queue_result(row, rows)["local_queue"],
                )
            snapshot = sha(contract.encode(rows))
            offset = 0
            if cursor is not None:
                match = re.fullmatch(r"([a-f0-9]{64}):([0-9]{1,6})", cursor)
                if match is None or match[1] != snapshot:
                    raise ValueError(
                        "Queue changed; restart inspection without cursor."
                    )
                offset = int(match[2])
                if offset > len(rows):
                    raise ValueError("Invalid queue cursor; restart inspection.")
            page = []
            for row in rows[offset : offset + 64]:
                check_request()
                request = row["request"]
                params = request["params"]
                page.append(
                    {
                        "request_id": request["request_id"],
                        "method": request["method"],
                        "kind": params["kind"],
                        "id": params["id"],
                        "expected_revision": params.get("expected_revision"),
                        "local_queue": self._queue_result(row, rows)["local_queue"],
                    }
                )
            end = offset + len(page)
            return dict(
                base,
                status="snapshot",
                entries=page,
                next_cursor=f"{snapshot}:{end}" if end < len(rows) else None,
            )

    def _server_saved(self, response, request):
        if (
            not isinstance(response, dict)
            or response.get("request_id") != request["request_id"]
            or "error" in response
        ):
            return False
        receipt = response.get("persistence")
        value = response.get("result")
        params = request["params"]
        if request["method"] == "memory_event_submit":
            return contract.core_receipt(response, request)
        if not isinstance(receipt, dict) or not isinstance(value, dict):
            return False
        native = (
            params["kind"] == "memory"
            and receipt.get("storage") == "tencent-l1"
            and isinstance(receipt.get("checkpoint"), str)
            and re.fullmatch(r"[a-f0-9]{64}", receipt["checkpoint"])
        )
        if not native and (
            not isinstance(receipt.get("commit"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", receipt["commit"])
        ):
            return False
        remote = (
            receipt.get("state") == "remote-confirmed"
            and receipt.get("durable") is True
            and receipt.get("github_confirmed", True) is True
            and receipt.get("local_durable", True) is True
        )
        local = (
            receipt.get("state") == "local-confirmed"
            and receipt.get("durable") is False
            and receipt.get("local_durable") is True
            and receipt.get("github_confirmed") is False
        )
        if not (remote or local):
            return False
        try:
            self._validate_target(value, params["kind"], params["id"])
        except Exception:
            return False
        if request["method"] == "content_set_enabled":
            return (
                value["record"] is not None
                and value["record"]["enabled"] is params["enabled"]
            )
        return same_record(
            params["kind"],
            params.get("record"),
            value["record"],
            preserve_enabled=False,
        )

    def _queue_result(self, row, rows=None):
        result = (
            row["response"]
            if row["state"] != "pending"
            else {
                "request_id": row["request"]["request_id"],
                "error": {"code": "content_upload_queued", "retryable": True},
                "persistence": {"durable": False},
            }
        )
        # Derived diagnostics only: never edit a retained request, its CAS base,
        # reconciliation references or ID to make a queued write go through.
        blockers: list[dict[str, str]] = []
        if blockers or row["state"] == "conflict":
            next_action = (
                "Inspect blockers and retained local proposals with content_queue_inspect, then read the current target with content_read. "
                "Resolve conflicts with an explicitly reviewed content_put using a new stable request_id, the current expected_revision and reconciles.request_ids selecting the exact conflicts. "
                "A blocked request requires review of its original error and an explicit identical retry after its cause is fixed. "
                "Fresh CAS writes can proceed independently; reconciliation is optional evidence tracking for reviewed merges. "
                "Do not delete queue entries, change their CAS bases or use a new ID merely to retry an existing write."
            )
        elif row["state"] == "blocked":
            next_action = "Inspect the original request and error with content_queue_inspect, fix its cause, then explicitly retry the exact saved request and request_id. Refresh does not retry blocked writes."
        elif row["state"] == "pending":
            next_action = "Server persistence is unconfirmed. Retry the exact saved request and request_id after restoring connectivity; do not create a replacement write or change its CAS base."
        elif row["state"] == "reconciled":
            next_action = "This proposal was resolved by a confirmed reconciliation. Keep the original evidence; do not resubmit it."
        else:
            next_action = "The Server receipt determines persistence; local mapping and running model context are separate."
        receipt = result.get("persistence", {})
        if (
            row["state"] == "confirmed"
            and row["request"]["method"] != "memory_event_submit"
        ):
            backup = receipt.get("github_backup", {})
            if not isinstance(backup, dict):
                backup = {}
            retry = backup.get("automatic_retry", {})
            if not isinstance(retry, dict):
                retry = {}
            confirmed = (
                receipt.get("state") == "remote-confirmed"
                and receipt.get("durable") is True
            )
            attention = (
                backup.get("attention_required") is True
                or backup.get("state") == "review-required"
                or retry.get("state") in ("paused", "review-required")
            )
            if confirmed:
                next_action = "Saved on the Server and GitHub backup confirmed. No further save action is needed."
            elif attention:
                next_action = "Saved on the Server. GitHub backup needs attention; inspect the backup diagnostic. Keep the saved request; do not create another write or poll for confirmation."
            elif retry.get("state") == "waiting":
                next_action = "Saved on the Server. GitHub backup is a separate background task. No additional write or polling is needed."
            else:
                next_action = "Saved on the Server. GitHub backup is not yet confirmed. Do not create another write to save the same content."
            # Only a validated Server receipt can produce this success summary.
            # Keep the original receipt and diagnostics unchanged below it.
            result = dict(
                write_status={
                    "status": "saved",
                    "message": next_action,
                    "backup_status": "confirmed"
                    if confirmed
                    else "needs-attention"
                    if attention
                    else "pending",
                },
                **result,
            )
        return dict(
            result,
            local_queue={
                "state": row["state"],
                "saved": True,
                "request_sha256": row["request_sha"],
                "server_saved": row["state"] == "confirmed",
                "github_confirmed": receipt.get("state") == "remote-confirmed"
                and receipt.get("durable") is True,
                "blocked_by": blockers,
                "resolution_request_id": row["resolution_request_id"],
                "next_action": next_action,
            },
        )
