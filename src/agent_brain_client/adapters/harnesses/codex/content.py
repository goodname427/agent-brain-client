"""Opt-in Codex content cache/native mapping; no Core, Git or secret discovery.

The embedding approved transport owns authentication. Refresh at startup/tool
boundaries is automatic when this candidate is wired; no new daemon is created.
"""

from agent_brain_client.domain.errors import CacheBlocked, CollectionCapacityExceeded
from agent_brain_client.application.synchronization.pull import ContentSynchronization
from agent_brain_client.adapters.harnesses.codex.sources import CodexSourceImports
from agent_brain_client.adapters.harnesses.codex.mapping import CodexContentMapping
from agent_brain_client.adapters.harnesses.codex.content_limits import (
    OFFLINE_TRANSPORT_ERRORS,
    SNAPSHOT_BYTES,
    sha,
    cache_bytes,
)
import os
from pathlib import Path
import re
import time
import uuid

BASE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[5]
import agent_brain_client.domain.contract as contract
from agent_brain_client.adapters.platforms.filesystem import (
    require_platform,
)
import agent_brain_client.adapters.platforms.filesystem as brain_lock

cache_mode = brain_lock.cache_mode
cache_directory = brain_lock.cache_directory
locked = brain_lock.cache_locked
from agent_brain_client.application.control import check_request, RequestStopped

# independent of the number of records or pages, with 64 KiB per record.
# Per-record envelopes fit in 512 bytes. Aggregate persistence must accommodate
# that contract, rather than treating a whole collection as one small record.
# Always-on rules appear both in their own file and in the managed AGENTS block.
# Retain room for the existing bounded native AGENTS preimage and guidance.
# Recovery stores base64 preimages twice, targets, optional Windows rollback
# images and path/digest metadata. Keep the same bound for writing and recovery.

from agent_brain_client.adapters.storage.content import (
    FileContentOutbox,
    operation_lock,
    operation_stage,
)


class CodexContentSession(FileContentOutbox):
    _cache_bytes = staticmethod(cache_bytes)
    _outbox_lock = staticmethod(operation_lock)
    _outbox_stage = staticmethod(operation_stage)

    @staticmethod
    def _outbox_locked(*args, **kwargs):
        return locked(*args, **kwargs)

    def __init__(self, home, cache, policy, client, health=None, codex_home=None):
        require_platform(
            "cache-storage", (getattr(client, "grant", None) or {}).get("platform")
        )
        native = Path(
            codex_home or os.environ.get("CODEX_HOME", str(Path(home) / ".codex"))
        ).expanduser()
        if not native.is_absolute() or native.is_symlink():
            raise CacheBlocked()
        self.native_home = native.resolve()
        self.native_memory_home = native if os.name == "nt" else self.native_home
        super().__init__(
            home,
            cache,
            policy,
            client,
            binding={
                "device_home": str(Path(home).resolve()),
                "codex_home": str(self.native_home),
            },
            protected=[
                Path(home).resolve() / name
                for name in (".codex", ".agents", ".config/agent-brain-client/content")
            ],
        )
        self.health = health or (lambda: None)
        self.offline_denied = False
        self.sources = CodexSourceImports(self)
        self.mapping = CodexContentMapping(self)
        self.synchronization = ContentSynchronization(self)

    def ensure_refresh_available(self):
        self._require_storage()

    def refresh_transaction(self):
        return self._outbox_locked(self.cache / "content.lock")

    def replay_writes(self):
        self._drain()

    def _request(self, method, params):
        response = self.client.request(
            {"request_id": uuid.uuid4().hex, "method": method, "params": params}
        )
        if "error" in response:
            error = response["error"]
            race = (
                method == "content_catalog"
                and isinstance(error, dict)
                and error.get("code") == "catalog_changed"
            )
            if not race and (
                not isinstance(error, dict)
                or error.get("retryable") is not True
                or error.get("code") not in OFFLINE_TRANSPORT_ERRORS
            ):
                self._deny_offline_snapshot()
            raise CacheBlocked()
        if not isinstance(response.get("result"), dict):
            raise CacheBlocked()
        return response["result"]

    def _read_target(self, kind, identity):
        value = self._request("content_read", {"kind": kind, "id": identity})
        self._validate_target(value, kind, identity)
        return value

    @staticmethod
    def _validate_target(value, kind, identity):
        if (
            not isinstance(value, dict)
            or set(value) != {"content_api_version", "kind", "id", "revision", "record"}
            or value["content_api_version"] != 1
            or (value["kind"], value["id"]) != (kind, identity)
            or not isinstance(value["revision"], str)
            or not re.fullmatch(r"[a-f0-9]{64}|missing", value["revision"])
        ):
            raise CacheBlocked()
        if value["record"] is not None:
            contract.record(kind, value["record"], identity)
        # Memory CAS covers canonical Core metadata/forgotten markers, not JSON.
        if kind != "memory" and value != contract.envelope(
            kind, identity, value["record"]
        ):
            raise CacheBlocked()

    def fetch_snapshot(self):
        kinds = self.policy["kinds"]
        catalog = self._catalog(kinds)
        # Reuse only a fully verified snapshot bound to this service and policy.
        # The live catalog revision, not the previous commit, determines reuse.
        cached = {}
        active = self._load("active.json")
        pin = self._load("offline-snapshot.json")
        if (
            isinstance(active, dict)
            and set(active) == {"commit", "snapshot_sha"}
            and isinstance(active["commit"], str)
            and re.fullmatch(r"[a-f0-9]{40}", active["commit"])
            and isinstance(pin, dict)
            and pin.get("schema") == 1
            and pin.get("policy_sha") == self.policy_sha
            and pin.get("service_sha") == self.queue_sha
            and pin.get("active") == active
            and not pin.get("read_denied")
        ):
            previous = self._load("snapshots/" + active["commit"] + ".json")
            if (
                isinstance(previous, dict)
                and set(previous) == {"content_api_version", "commit", "records"}
                and previous["content_api_version"] == 1
                and previous["commit"] == active["commit"]
                and sha(contract.encode(previous)) == active["snapshot_sha"]
                and len(contract.encode(previous)) <= SNAPSHOT_BYTES
                and isinstance(previous["records"], list)
            ):
                for value in previous["records"]:
                    self._validate_target(value, value.get("kind"), value.get("id"))
                    kind, identity = value["kind"], value["id"]
                    contract.validate("content_read", {"kind": kind, "id": identity})
                    if (
                        kind not in kinds
                        or not self._allows(kind, identity)
                        or (kind, identity) in cached
                    ):
                        raise CacheBlocked()
                    cached[kind, identity] = value
        records = []
        seen = set()
        for item in catalog["artifacts"]:
            if not isinstance(item, dict) or set(item) != {"kind", "id", "revision"}:
                raise CacheBlocked()
            kind, identity = item["kind"], item["id"]
            contract.validate("content_read", {"kind": kind, "id": identity})
            if kind not in kinds or (kind, identity) in seen:
                raise CacheBlocked()
            seen.add((kind, identity))
            # Keep the complete catalog for the final snapshot race check, but
            # never fetch or map content outside this device's selected IDs.
            if not self._allows(kind, identity):
                continue
            value = cached.get((kind, identity))
            if value is None or value["revision"] != item["revision"]:
                value = self._request("content_read", {"kind": kind, "id": identity})
            if (
                set(value)
                != {"content_api_version", "kind", "id", "revision", "record"}
                or value["content_api_version"] != 1
                or value["kind"] != kind
                or value["id"] != identity
                or value["revision"] != item["revision"]
                or not re.fullmatch(r"[a-f0-9]{64}", value["revision"])
            ):
                raise CacheBlocked()
            contract.record(kind, value["record"], identity)
            records.append(value)
        if self._catalog(kinds) != catalog:
            raise CacheBlocked()
        snapshot = {
            "content_api_version": 1,
            "commit": catalog["commit"],
            "records": records,
        }
        if len(contract.encode(snapshot)) > SNAPSHOT_BYTES:
            raise CollectionCapacityExceeded()
        return snapshot

    def _catalog(self, kinds):
        cursor = None
        commit = None
        artifacts = []
        seen = set()
        size = 0
        while True:
            page = self._request(
                "content_catalog",
                dict(kinds=kinds, **({"cursor": cursor} if cursor else {})),
            )
            try:
                contract.validate_catalog(page)
            except ValueError:
                raise CacheBlocked() from None
            if commit is not None and page["commit"] != commit:
                raise CacheBlocked()
            commit = page["commit"]
            for row in page["artifacts"]:
                key = (row.get("kind"), row.get("id"))
                if key in seen:
                    raise CacheBlocked()
                seen.add(key)
                artifacts.append(row)
                size += len(contract.encode(row))
                if size > SNAPSHOT_BYTES:
                    raise CollectionCapacityExceeded()
            next_cursor = page.get("next_cursor")
            if next_cursor is None:
                return {
                    "content_api_version": 1,
                    "commit": commit,
                    "artifacts": artifacts,
                }
            if next_cursor == cursor:
                raise CacheBlocked()
            cursor = next_cursor

    def _deny_offline_snapshot(self):
        # Preserve downloaded content, but do not mask a known service refusal
        # with a later transport timeout, including after process restart.
        self.offline_denied = True
        try:
            pin = self._load("offline-snapshot.json")
            if (
                isinstance(pin, dict)
                and pin.get("policy_sha") == self.policy_sha
                and pin.get("service_sha") == self.queue_sha
                and pin.get("read_denied") is not True
            ):
                self._save("offline-snapshot.json", dict(pin, read_denied=True))
        except Exception:
            pass  # The current call still refuses cached results.

    def _pin_offline_snapshot(self, snapshot):
        # A policy alone does not bind cached records to a service/principal.
        # Pin only after an authenticated remote fetch and owned-file checks.
        active = {
            "commit": snapshot["commit"],
            "snapshot_sha": sha(contract.encode(snapshot)),
        }
        identity = {
            "schema": 1,
            "policy_sha": self.policy_sha,
            "service_sha": self.queue_sha,
            "active": active,
        }
        previous = self._load("offline-snapshot.json")
        if (
            isinstance(previous, dict)
            and {k: v for k, v in previous.items() if k != "verified_at"} == identity
            and type(previous.get("verified_at")) in (int, float)
            and 0 < previous["verified_at"] < float("inf")
        ):
            self.offline_denied = False
            return
        self._save("offline-snapshot.json", dict(identity, verified_at=time.time()))
        self.offline_denied = False

    def _offline_read(self, request):
        """Read a prior verified snapshot, never infer remote absence or success."""

        def unavailable(code, retryable):
            return {
                "request_id": request["request_id"],
                "error": {"code": code, "retryable": retryable},
                "persistence": {"durable": False},
                "local_cache": {"available": False},
            }

        try:
            if (
                self.offline_denied
                or request["method"] not in ("content_read", "content_catalog")
                or request["method"] not in self.methods()
            ):
                raise CacheBlocked()
            with locked(self.cache / "content.lock", cancel_check=check_request):
                active = self._load("active.json")
                if active is None:
                    return unavailable("content_offline_cache_miss", True)
                if (
                    not isinstance(active, dict)
                    or set(active) != {"commit", "snapshot_sha"}
                    or not re.fullmatch(r"[a-f0-9]{40}", active.get("commit", ""))
                    or not re.fullmatch(r"[a-f0-9]{64}", active.get("snapshot_sha", ""))
                ):
                    raise CacheBlocked()
                pin = self._load("offline-snapshot.json")
                identity = {
                    "schema": 1,
                    "policy_sha": self.policy_sha,
                    "service_sha": self.queue_sha,
                    "active": active,
                }
                if (
                    not isinstance(pin, dict)
                    or set(pin) != set(identity) | {"verified_at"}
                    or {k: v for k, v in pin.items() if k != "verified_at"} != identity
                    or type(pin["verified_at"]) not in (int, float)
                    or not 0 < pin["verified_at"] < float("inf")
                ):
                    raise CacheBlocked()
                journal = self._load("transaction.json")
                if journal and journal.get("phase") not in ("complete", "rolled-back"):
                    raise CacheBlocked()
                self.mapping.owned_snapshot()
                queue = self.queue_status()
                snapshot = self._load("snapshots/" + active["commit"] + ".json")
                if (
                    not isinstance(snapshot, dict)
                    or set(snapshot) != {"content_api_version", "commit", "records"}
                    or snapshot["content_api_version"] != 1
                    or snapshot["commit"] != active["commit"]
                    or sha(contract.encode(snapshot)) != active["snapshot_sha"]
                    or len(contract.encode(snapshot)) > SNAPSHOT_BYTES
                    or not isinstance(snapshot["records"], list)
                ):
                    raise CacheBlocked()
                seen = set()
                for value in snapshot["records"]:
                    self._validate_target(value, value.get("kind"), value.get("id"))
                    kind, identity = value["kind"], value["id"]
                    contract.validate("content_read", {"kind": kind, "id": identity})
                    if (
                        kind not in self.policy["kinds"]
                        or not self._allows(kind, identity)
                        or (kind, identity) in seen
                    ):
                        raise CacheBlocked()
                    contract.record(kind, value["record"], identity)
                    seen.add((kind, identity))
                params = request["params"]
                result: dict | None
                if request["method"] == "content_catalog":
                    result = {
                        "content_api_version": 1,
                        "commit": snapshot["commit"],
                        "artifacts": [
                            {
                                "kind": v["kind"],
                                "id": v["id"],
                                "revision": v["revision"],
                            }
                            for v in snapshot["records"]
                            if v["kind"] in params["kinds"]
                        ],
                    }
                    result = contract.catalog_page(
                        result, params.get("cursor"), [self.queue_sha, params["kinds"]]
                    )
                else:
                    result = next(
                        (
                            v
                            for v in snapshot["records"]
                            if (v["kind"], v["id"]) == (params["kind"], params["id"])
                        ),
                        None,
                    )
                    if result is None:
                        return unavailable("content_offline_cache_miss", True)
                return {
                    "request_id": request["request_id"],
                    "result": result,
                    "persistence": {"durable": False, "state": "read-only"},
                    "local_cache": {
                        "available": True,
                        "stale": True,
                        "remote_confirmed": False,
                        "snapshot_commit": snapshot["commit"],
                        "verified_at": pin["verified_at"],
                        "queue": queue,
                    },
                }
        except RequestStopped:
            raise
        except Exception:
            return unavailable("content_offline_cache_unverified", False)

    def _read_with_cache(self, request):
        try:
            result = self.client.request(request)
        except OSError:
            result = {
                "request_id": request["request_id"],
                "error": {"code": "transport_unconfirmed", "retryable": True},
                "persistence": {"durable": False},
            }
        check_request()
        # Authentication, scope, credential/identity failures and invalid responses
        # must remain errors even if an old local snapshot exists.
        error = result.get("error", {}) if isinstance(result, dict) else {}
        if (
            isinstance(result, dict)
            and result.get("request_id") == request["request_id"]
            and isinstance(error, dict)
            and error.get("retryable") is True
            and error.get("code") in OFFLINE_TRANSPORT_ERRORS
        ):
            return self._offline_read(request)
        if (
            request["method"] == "content_catalog"
            and isinstance(error, dict)
            and error.get("code") == "catalog_changed"
            and isinstance(result, dict)
            and result.get("request_id") == request["request_id"]
        ):
            return result
        if isinstance(result, dict) and "error" in result:
            self.offline_denied = True
            try:
                with locked(self.cache / "content.lock", cancel_check=check_request):
                    self._deny_offline_snapshot()
            except RequestStopped:
                raise
            except Exception:
                pass
        return result

    def refresh(self, *, replay=True):
        return self.synchronization.pull(replay=replay)

    def recall(self, request):
        """Read only; do not drain queues, refresh mappings or broaden grants."""
        check_request()
        if (
            "memory_query" not in self.methods()
            or "memory" not in self.policy["kinds"]
            or "memory" not in self.client.grant["artifact_types"]
        ):
            raise CacheBlocked()
        contract.global_recall_params(request["params"])
        reader = getattr(self.client, "recall", None)
        if not callable(reader):
            return {
                "request_id": request["request_id"],
                "error": {
                    "code": "layered_recall_transport_unavailable",
                    "retryable": False,
                },
                "persistence": {"durable": False},
            }
        result = reader(request)
        if (
            not isinstance(result, dict)
            or result.get("request_id") != request["request_id"]
        ):
            raise CacheBlocked()
        if "error" not in result:
            contract.global_recall_result(
                result["result"], request["params"], self.client.device_id
            )
        if result.get("error", {}).get("code") == "global_source_device_denied":
            result = dict(
                result,
                source_admission={
                    "status": "device-source-approval-required",
                    "next_action": "Check this enrolled device in the existing Server global source registry and apply its exact approved metadata packet. Do not reuse another device identity or change credentials.",
                },
            )
        return dict(
            result,
            local_refresh={
                "status": "read-only",
                "native_changed": False,
                "model_loaded": "unknown",
            },
        )

    def invoke(self, request, reconciles=None, *, event_gate=None):
        check_request()
        require_platform("cache-storage", self.platform)
        method = request.get("method")
        params = request.get("params")
        contract.validate(method, params)
        if method not in self.methods():
            raise CacheBlocked()
        if method == "memory_query":
            if (
                "memory" not in self.policy["kinds"]
                or "memory" not in self.client.grant["artifact_types"]
            ):
                raise CacheBlocked()
            try:
                response = self.client.request(request)
            except OSError:
                response = {
                    "request_id": request["request_id"],
                    "error": {"code": "memory_query_unavailable", "retryable": True},
                    "persistence": {"durable": False},
                }
            if (
                not isinstance(response, dict)
                or response.get("request_id") != request["request_id"]
            ):
                raise CacheBlocked()
            if "error" in response:
                return response
            value = response.get("result")
            if (
                not isinstance(value, dict)
                or set(value)
                != {"content_api_version", "commit", "method", "time_basis", "topics"}
                or value["content_api_version"] != 1
                or value["time_basis"] != "memory_updated_at"
                or value["method"] not in ("lexical", "recent-updates")
                or not isinstance(value["commit"], str)
                or not re.fullmatch(r"[a-f0-9]{40}", value["commit"])
                or not isinstance(value["topics"], list)
                or len(value["topics"]) > params["limit"]
            ):
                raise CacheBlocked()
            seen = set()
            for topic in value["topics"]:
                if not isinstance(topic, dict) or set(topic) != {
                    "id",
                    "title",
                    "description",
                    "revision",
                    "updated_at",
                    "score",
                    "preview",
                }:
                    raise CacheBlocked()
                contract.validate("content_read", {"kind": "memory", "id": topic["id"]})
                if (
                    topic["id"] in seen
                    or not self._allows("memory", topic["id"])
                    or not isinstance(topic["revision"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", topic["revision"])
                    or type(topic["score"]) is not int
                    or topic["score"] < 0
                    or any(
                        not isinstance(topic[k], str) or len(topic[k]) > 1024
                        for k in ("title", "description")
                    )
                    or not isinstance(topic["preview"], str)
                    or len(topic["preview"]) > 320
                ):
                    raise CacheBlocked()
                contract.query_time(topic["updated_at"])
                seen.add(topic["id"])
            return dict(
                response,
                local_refresh={
                    "status": "query-only",
                    "native_changed": False,
                    "model_loaded": "unknown",
                },
            )
        kinds = params["kinds"] if method == "content_catalog" else [params["kind"]]
        if (
            any(kind not in self.policy["kinds"] for kind in kinds)
            or method != "content_catalog"
            and not self._allows(params["kind"], params["id"])
        ):
            raise CacheBlocked()
        if method in contract.WRITES:
            result = self.invoke_write(request, reconciles, event_gate=event_gate)
            if result.get("local_refresh", {}).get("status") == "event-disabled":
                return result
        else:
            # A live read already goes through the authenticated Server. Mapping
            # every artifact twice here turned a batch into quadratic transport.
            # Startup and confirmed writes retain the transactional full refresh.
            result = self._read_with_cache(request)
            if (
                not isinstance(result, dict)
                or result.get("request_id") != request["request_id"]
            ):
                raise CacheBlocked()
            if "error" not in result and method == "content_read":
                self._validate_target(
                    result.get("result"), params["kind"], params["id"]
                )
        if method == "content_catalog" and "error" not in result:
            value = result.get("result", {})
            try:
                contract.validate_catalog(value)
            except ValueError:
                raise CacheBlocked() from None
            visible = []
            seen = set()
            for item in value["artifacts"]:
                if not isinstance(item, dict) or set(item) != {
                    "kind",
                    "id",
                    "revision",
                }:
                    raise CacheBlocked()
                contract.validate(
                    "content_read", {"kind": item["kind"], "id": item["id"]}
                )
                identity = (item["kind"], item["id"])
                if (
                    identity in seen
                    or item["kind"] not in kinds
                    or not re.fullmatch(r"[a-f0-9]{64}", item["revision"])
                ):
                    raise CacheBlocked()
                seen.add(identity)
                if item["kind"] in kinds and self._allows(item["kind"], item["id"]):
                    visible.append(item)
            result = dict(result, result=dict(value, artifacts=visible))
        if method not in contract.WRITES:
            refresh = {
                "status": "read-only",
                "native_changed": False,
                "model_loaded": "unknown",
                "next_action": "Mappings refresh at MCP startup and after confirmed writes; this read does not refresh native mappings.",
            }
        else:
            # This write already drained once. Mapping must not silently retry
            # another request whose acknowledgement was lost in that drain.
            refresh = (
                (
                    {
                        "status": "event-only",
                        "native_changed": False,
                        "model_loaded": "unknown",
                    }
                    if method == "memory_event_submit"
                    else self.refresh(replay=False)
                )
                if result.get("local_queue", {}).get("state") == "confirmed"
                else {
                    "status": "content-local-changes-retained",
                    "native_changed": False,
                    "model_loaded": "unknown",
                    "queue": self.queue_status(),
                }
            )
        return dict(result, local_refresh=refresh)
