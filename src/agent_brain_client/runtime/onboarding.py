"""Compose one historical import around the existing authenticated event queue."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import time

from agent_brain_client.adapters.platforms.filesystem import cache_locked
from agent_brain_client.adapters.storage import metadata
from agent_brain_client.adapters.storage.project_settings import source_active
from agent_brain_client.application.control import check_request, request_timeout
from agent_brain_client.application.memory.onboarding import SOURCE_SCOPE, Summary, authorization, excluded_original
from agent_brain_client.domain import contract
from agent_brain_client.domain.errors import CacheBlocked
from agent_brain_client.domain.events import NATIVE_MEMORY_CONTRACT, native_memory_event
from agent_brain_client.domain.receipts import core_receipt
from agent_brain_client.domain.scopes import digest, namespace, ordinary_drive_path, origin, select
from agent_brain_client.runtime import collection


def enrolled_origin(home, path, core):
    value = core.load(home)
    local = core.eligible(value, path)
    if (
        local is None or local["cwd"] != ordinary_drive_path(path)
        or core.legacy(value, local) is not None
    ):
        return None
    return value, local


def personal_origin(home, harness, value, local):
    selected = select(value["scope"], local)
    return selected is not None and selected["sharing"] == "personal" and source_active(
        home, local["cwd"], harness,
    )


def onboard_native_memory(adapter, arguments):
    """Accept a bounded local approval snapshot without waiting for any HTTP call."""
    from agent_brain_client.runtime.delivery import register_task, wake

    harness, endpoint, actor, path, resume, approval_id = authorization(arguments)
    if harness != adapter.host.name or not Path(path).is_absolute():
        raise ValueError("Explicit bound host and destination directory required")
    try:
        route = enrolled_origin(adapter.home, path, collection)
    except (OSError, ValueError):
        route = None
    if route is None:
        return {"error": {"code": "onboarding-origin-unavailable"}}
    value, local = route
    if not personal_origin(adapter.home, harness, value, local):
        return {"error": {"code": "onboarding-personal-origin-required"}}

    client, queue = collection.build_delivery_queue(value, local, harness)
    if (
        client.base.profile["endpoint"].rstrip("/") != endpoint
        or client.actor != actor
        or adapter.client.queue_identity != client.base.queue_identity
    ):
        return {"error": {"code": "onboarding-destination-changed"}}
    project = namespace(client.actor, value["scope"]["profile_id"])
    group = "native-source-" + digest(adapter.binding)
    # The slow worker lease never covers acceptance/status.
    with cache_locked(queue.cache / "onboarding-approval.lock"):
        batch = approval_snapshot(adapter.sources, queue, client.scope_sha, group, project, resume, approval_id)
        if batch is None:
            return {"error": {"code": "onboarding-approval-unavailable"}}
        approval_id, inventory = batch

        progress = progress_name(approval_id)
        state = queue._load(progress)
        if state is None:
            summary = Summary(harness)
            summary.counts["pending"] = len(inventory["sources"])
            state = {
                "started": time.time_ns(),
                "summary": dict(summary.result(inventory["status"]), status="onboarding-queued"),
            }
            queue._save(progress, state)
    if state["summary"]["status"] == "onboarding-queued" or state["summary"]["counts"]["pending"]:
        task = {
            "schema": 1, "harness": "onboarding", "source_harness": harness,
            "origin": local, "approval_id": approval_id,
        }
        register_task(adapter.home, digest(task), task, wake_worker=False)
        try:
            wake(adapter.home)
        except OSError:
            # The ordinary supervisor can discover the durable task after a
            # spawn failure or client death; approval is already recorded.
            pass
    return dict(state["summary"], approval_id=approval_id)


def progress_name(approval_id):
    return "onboarding/" + approval_id + "-progress.json"


def enrolled_sources(home, harness, client, queue):
    """Reopen only the enrolled host's native source port, never a supplied path."""
    from agent_brain_client.adapters.storage import registration
    from agent_brain_client.adapters.storage.content_session import ContentSession
    from agent_brain_client.application.tools.catalog import memory_sources_authorized
    from agent_brain_client.domain.authorization import permits_scope
    from agent_brain_client.runtime.harness_content import content_host

    _, policy = registration.registration(home)
    host = content_host(harness)
    native, cache = host.location(home, policy["runtime"])
    grant = client.base.grant
    if not memory_sources_authorized(queue.policy, grant) or not permits_scope(
        grant["scopes"], "memory_event_submit",
    ):
        raise CacheBlocked()
    session = ContentSession.metadata_view(
        home, native, cache, queue.policy, platform=grant["platform"], host=host,
    )
    return session.sources


def deliver_approved_batch(home, task, core):
    """Existing detached delivery worker, with a fresh route check before disclosure."""
    if (
        set(task) != {"schema", "harness", "source_harness", "origin", "approval_id"}
        or task["schema"] != 1 or task["harness"] != "onboarding"
        or task["source_harness"] not in ("codex", "codebuddy")
        or not isinstance(task["approval_id"], str)
        or not re.fullmatch("[a-f0-9]{64}", task["approval_id"])
    ):
        raise ValueError("Bound Onboarding delivery task required")
    harness, local, approval_id = (task[k] for k in ("source_harness", "origin", "approval_id"))
    path = local["cwd"]
    route = enrolled_origin(home, path, core)
    if route is None or route[1] != local or not personal_origin(home, harness, route[0], local):
        return True, False
    value = route[0]
    client, queue = core.build_delivery_queue(value, local, harness)
    sources = enrolled_sources(home, harness, client, queue)
    group = "native-source-" + digest(sources.session.binding)
    project = namespace(client.actor, value["scope"]["profile_id"])
    progress = progress_name(approval_id)

    def allowed():
        current = collection.load(home, verify_code=False)
        return (
            current["scope"] == value["scope"]
            and core.eligible(current, path) == local
            and core.legacy(current, local) is None
            and personal_origin(home, harness, current, local)
        )

    # Only a worker holds this long lock; one native queue owner covers all
    # enrolled origins of this harness to retain cross-origin exact receipts.
    with cache_locked(queue.cache.parent / "onboarding.lock"):
        state = queue._load(progress)
        if state is None:
            raise CacheBlocked()
        summary = state["summary"]
        previous = summary["counts"]
        if summary["status"] != "onboarding-queued" and not previous["pending"]:
            return False, False
        batch = approval_snapshot(sources, queue, client.scope_sha, group, project, True, approval_id)
        if batch is None:
            raise CacheBlocked()
        _, inventory = batch
        summary = process_batch(
            sources, harness, client, queue, project, group, inventory, allowed, started=state["started"],
        )
        queue._save(progress, dict(state, summary=summary))
        counts = summary["counts"]
        advanced = sum(counts[k] for k in ("received", "already_exists")) > sum(
            previous[k] for k in ("received", "already_exists")
        )
        return counts["pending"] > 0, advanced


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


def process_batch(sources, harness, client, queue, project, group, inventory, allowed, *, started):
    summary = Summary(harness)
    excluded_sources = sources.onboarding_exclusions()
    wanted = {
        "native_memory_" + digest([project, group, item["source_id"], item["source_fingerprint"]])
        for item in inventory["sources"] if item["status"] == "not-checked"
    }
    existing, other_routes = retained_onboarding_events(queue, group, project, wanted)
    transport_unconfirmed = False
    source_admitted = None
    for index, item in enumerate(inventory["sources"]):
        if request_timeout(90) <= 4:
            summary.counts["pending"] += len(inventory["sources"]) - index
            break
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
            recorded = response.get("result", {}).get("replayed") is not True
            outcome = "received" if retained["created"] >= started and recorded else "already_exists"
            summary.add(outcome, response=response)
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
                body, _ = sources.read_onboarding_original(source_id, fingerprint)
                if body != request["params"]["event"]["content"]:
                    raise CacheBlocked()
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
                summary.add("failed" if exclusion == "original-outside-bound" else "excluded", exclusion)
                continue
            if not allowed():
                summary.add("pending", "onboarding-origin-changed")
                continue
            if source_admitted is None and not transport_unconfirmed and request_timeout(90) > 4:
                try:
                    source_admitted = client.status(group).get("source_admitted") is True
                except (OSError, ValueError):
                    transport_unconfirmed = True
            if source_admitted is False:
                summary.add("pending", "server-source-paused-or-denied")
                continue
            if transport_unconfirmed or request_timeout(90) <= 4:
                summary.counts["pending"] += len(inventory["sources"]) - index
                summary.reasons["delivery-deferred"] += 1
                break
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
                summary.add("pending", "server-source-paused-or-denied")
            else:
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
