"""Compose one historical import around the existing authenticated event queue."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_brain_client.adapters.platforms.filesystem import cache_locked
from agent_brain_client.adapters.storage import metadata
from agent_brain_client.adapters.storage.project_settings import source_active
from agent_brain_client.application.control import check_request, request_timeout
from agent_brain_client.application.memory.onboarding import SOURCE_SCOPE, Summary, authorization, excluded_original
from agent_brain_client.domain import contract
from agent_brain_client.domain.errors import CacheBlocked, EventDiagnosticsUnavailable
from agent_brain_client.domain.events import NATIVE_MEMORY_CONTRACT, native_memory_event
from agent_brain_client.domain.receipts import core_receipt
from agent_brain_client.domain.scopes import digest, namespace, ordinary_drive_path, origin, select
from agent_brain_client.runtime import collection


def onboard_native_memory(adapter, arguments):
    """Stage only eligible host originals and send through the enrolled L0 route."""
    harness, endpoint, actor, path, resume, approval_id = authorization(arguments)
    if harness != adapter.host.name or not Path(path).is_absolute():
        raise ValueError("Explicit bound host and destination directory required")
    try:
        value = collection.load(adapter.home)
        local = collection.eligible(value, path)
    except (OSError, ValueError):
        return {"error": {"code": "onboarding-origin-unavailable"}}
    if (
        local is None or local["cwd"] != ordinary_drive_path(path)
        or collection.legacy(value, local) is not None
    ):
        return {"error": {"code": "onboarding-origin-unavailable"}}

    scope = value["scope"]
    if select(scope, local)["sharing"] != "personal" or not source_active(adapter.home, path, harness):
        return {"error": {"code": "onboarding-personal-origin-required"}}

    client, queue = collection.build_delivery_queue(value, local, harness)
    if (
        client.base.profile["endpoint"].rstrip("/") != endpoint
        or client.actor != actor
        or adapter.client.queue_identity != client.base.queue_identity
    ):
        return {"error": {"code": "onboarding-destination-changed"}}

    # Preserve the event IDs and occurrence times used by the original importer.
    project = namespace(client.actor, scope["profile_id"])
    group = "native-source-" + digest(adapter.binding)

    def allowed():
        current = collection.load(adapter.home, verify_code=False)
        return (
            current["scope"] == scope
            and collection.eligible(current, path) == local
            and collection.legacy(current, local) is None
            and source_active(adapter.home, path, harness)
        )

    # One host queue owner at a time, including when two requests choose
    # different origins in the same personal namespace.
    with cache_locked(queue.cache.parent / "onboarding.lock"):
        if not allowed():
            return {"error": {"code": "onboarding-origin-changed"}}
        batch = approval_snapshot(adapter.sources, queue, client.scope_sha, group, project, resume, approval_id)
        if batch is None:
            return {"error": {"code": "onboarding-approval-unavailable"}}
        approval_id, inventory = batch
        summary = process_batch(adapter.sources, harness, client, queue, project, group, inventory, allowed)
        return dict(summary, approval_id=approval_id)


def approval_snapshot(sources, queue, route_scope_sha, group, project, resume, approval_id):
    binding = {
        "schema": 1, "scope": SOURCE_SCOPE, "queue_sha": queue.queue_sha,
        "source_group": group, "project_id": project, "route_scope_sha": route_scope_sha,
    }
    if resume:
        if approval_id is None:
            latest = queue._load("onboarding/latest.json")
            if latest is None:
                return None
            if not isinstance(latest, dict) or set(latest) != {"approval_id"}:
                raise CacheBlocked()
            approval_id = latest["approval_id"]
        if not isinstance(approval_id, str) or len(approval_id) != 64 or any(
            char not in "0123456789abcdef" for char in approval_id
        ):
            raise CacheBlocked()
        saved = queue._load("onboarding/" + approval_id + ".json")
        if saved is None:
            return None
        if (not isinstance(saved, dict) or set(saved) != {"binding", "inventory"}
                or saved["binding"] != binding or digest(saved) != approval_id):
            raise CacheBlocked()
        return approval_id, saved["inventory"]

    inventory = sources.provider.inventory(onboarding=True)
    snapshot = {field: inventory[field] for field in ("status", "sources")}
    saved = {"binding": binding, "inventory": snapshot}
    approval_id = digest(saved)
    entry = "onboarding/" + approval_id + ".json"
    prior = queue._load(entry)
    if prior is None:
        queue._save(entry, saved)
    elif prior != saved:
        raise CacheBlocked()
    queue._save("onboarding/latest.json", {"approval_id": approval_id})
    return approval_id, snapshot


def process_batch(sources, harness, client, queue, project, group, inventory, allowed):
    summary = Summary(harness)
    excluded_sources = sources.onboarding_exclusions()
    wanted = {
        "native_memory_" + digest([project, group, item["source_id"], item["source_fingerprint"]])
        for item in inventory["sources"] if item["status"] == "not-checked"
    }
    existing, other_routes = retained_onboarding_events(queue, group, project, wanted)
    transport_unconfirmed = False
    source_admitted = None
    for item in inventory["sources"]:
        check_request()
        source_id, fingerprint = item["source_id"], item["source_fingerprint"]
        if item["status"] != "not-checked":
            summary.add("failed" if item["status"] == "unavailable" else "excluded", item["reason"])
            continue

        key = digest([project, group, source_id, fingerprint])
        event_id, request_id = "native-" + key[:56], "native_memory_" + key
        retained = existing.get(request_id)
        other = other_routes.get(request_id)
        if retained and retained["state"] == "confirmed" and core_receipt(retained["response"], retained["request"]):
            response = retained["response"]
            if not transport_unconfirmed and request_timeout(90) > 4 and allowed():
                try:
                    status = client.status(group, event_id=event_id, event_only=True)
                    response = {"result": status} if status["capture_extraction"].get("l0_accepted") is True else response
                except (EventDiagnosticsUnavailable, OSError, ValueError, KeyError):
                    transport_unconfirmed = True
            summary.add("already_exists", response=response)
            continue
        if other:
            if other["state"] == "confirmed":
                summary.add("already_exists", response=other["response"])
            else:
                summary.add("failed", "prior-origin-receipt-unconfirmed")
            continue
        if source_id in excluded_sources:
            summary.add("excluded", "retained-exclusion")
            continue

        try:
            if retained is not None:
                request = retained["request"]
                body = request["params"]["event"]["content"]
            else:
                body, mtime_ns = sources.read_onboarding_original(source_id, fingerprint)
                original_time = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
                    milliseconds=mtime_ns // 1_000_000,
                )
                event = {
                    "contract": NATIVE_MEMORY_CONTRACT,
                    "event_id": event_id,
                    "project_id": project,
                    "session_id": group,
                    "source_id": source_id,
                    "source_fingerprint": fingerprint,
                    "occurred_at": original_time.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "content": body,
                }
                request = {
                    "request_id": request_id, "method": "memory_event_submit",
                    "params": {"kind": "memory", "id": event_id, "event": event},
                }
            exclusion = excluded_original(body)
            if exclusion:
                summary.add("excluded", exclusion)
                continue
            if not allowed():
                summary.add("failed", "onboarding-origin-changed")
                continue
            if source_admitted is None and not transport_unconfirmed and request_timeout(90) > 4:
                try:
                    source_admitted = client.status(group).get("source_admitted") is True
                except (OSError, ValueError):
                    transport_unconfirmed = True
            if source_admitted is False:
                summary.add("failed", "server-source-paused-or-denied")
                continue
            if transport_unconfirmed or request_timeout(90) <= 4:
                queue._enqueue(request)
                summary.add("pending", "delivery-deferred")
                continue
            response = queue.invoke(request, event_gate=allowed)
        except (CacheBlocked, OSError, ValueError, OverflowError):
            summary.add("failed", "source-or-queue-unavailable")
            continue

        state = response["local_queue"]["state"]
        if state == "confirmed" and core_receipt(
            {field: response[field] for field in ("request_id", "result", "persistence")}, request,
        ):
            outcome = "already_exists" if response["result"].get("replayed") is True else "received"
            summary.add(outcome, response=response)
        elif state == "pending":
            transport_unconfirmed = True
            summary.add("pending", "delivery-unconfirmed")
        else:
            if response.get("error", {}).get("code") in (
                "global_source_stopped", "global_origin_not_approved", "global_source_device_denied",
                "existing_project_capture_delegated", "global_scope_digest_mismatch",
            ):
                source_admitted = False
            summary.add("failed", "server-rejected-or-receipt-unavailable")
    return summary.result(inventory["status"])


def retained_onboarding_events(queue, group, project, wanted):
    """Respect receipts bound to a different prior origin without rebinding them."""
    def matches(row):
        request = row["request"]
        event = request["params"].get("event", {})
        if (
            event.get("contract") != NATIVE_MEMORY_CONTRACT
            or event["session_id"] != group
            or event["project_id"] != project
        ):
            return False
        native_memory_event(event)
        key = digest([project, group, event["source_id"], event["source_fingerprint"]])
        if request["request_id"] != "native_memory_" + key or event["event_id"] != "native-" + key[:56]:
            raise CacheBlocked()
        return True

    current = {
        row["request"]["request_id"]: row
        for row in queue._queued()
        if row["request"]["request_id"] in wanted and matches(row)
    }
    alternate = {}
    for origin_file in queue.cache.parent.glob("*/origin.json"):
        if origin_file.parent == queue.cache:
            continue
        check_request()
        root = metadata.canonical(origin_file.parent)
        outbox = metadata.canonical(root / "outbox")
        for path in outbox.glob("native_memory_*.json"):
            if path.stem not in wanted:
                continue
            saved_origin = origin(metadata.private_json(origin_file, cache=True))
            if root.name != digest(saved_origin):
                raise CacheBlocked()
            identity = dict(queue.client.queue_identity, origin_sha=digest(saved_origin))
            expected = metadata.digest(contract.encode({"policy_sha": queue.policy_sha, "service": identity}))
            row = metadata.private_json(path, cache=True)
            request = row["request"]
            queue._check_write(request)
            if (
                request["request_id"] != path.stem
                or row["policy_sha"] != expected
                or row["request_sha"] != metadata.digest(contract.encode(request))
                or not matches(row)
            ):
                raise CacheBlocked()
            if row["state"] == "confirmed" and core_receipt(row["response"], request):
                alternate[path.stem] = row
            elif path.stem not in alternate:
                alternate[path.stem] = {"state": "unconfirmed"}
    return current, alternate
