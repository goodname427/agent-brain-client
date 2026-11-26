"""Compose one historical import around the existing authenticated event queue."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import time
from uuid import uuid4

from agent_brain_client.adapters.platforms.filesystem import cache_locked
from agent_brain_client.adapters.storage import metadata
from agent_brain_client.adapters.storage.native_sources import source_inventory_status
from agent_brain_client.adapters.storage.project_settings import source_active
from agent_brain_client.application.control import check_request, request_timeout
from agent_brain_client.application.memory.onboarding import SOURCE_SCOPE, Summary, authorization
from agent_brain_client.domain import contract
from agent_brain_client.domain.errors import CacheBlocked, CollectionCapacityExceeded, GlobalRequestTooLarge
from agent_brain_client.domain.events import NATIVE_MEMORY_CONTRACT, l0_request, native_memory_content, native_memory_event
from agent_brain_client.domain.receipts import core_receipt
from agent_brain_client.domain.scopes import digest, namespace, ordinary_drive_path, origin, select
from agent_brain_client.runtime import collection


PAGE_ITEMS = 64
PAGE_BYTES = 512 * 1024


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
            summary.counts["pending"] = inventory["count"] if "head" in inventory else len(inventory["sources"])
            state = {
                "started": time.time_ns(),
                "track_receipts": True,
                "summary": dict(summary.result(inventory["status"]), status="onboarding-queued"),
            }
            if "head" in inventory:
                state.update(next_page=inventory["head"], completed=Summary(harness).result(inventory["status"]))
            queue._save(progress, state)
    if batch_pending(state["summary"]):
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


def batch_pending(summary):
    return summary["status"] == "onboarding-queued" or summary["counts"]["pending"] > 0


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
        if not batch_pending(summary):
            return False, False
        batch = approval_snapshot(sources, queue, client.scope_sha, group, project, True, approval_id)
        if batch is None:
            raise CacheBlocked()
        _, inventory = batch
        if "head" in inventory:
            updated = process_page(sources, harness, client, queue, project, group, inventory, allowed,
                                   approval_id=approval_id, state=state)
        else:
            summary = process_batch(
                sources, harness, client, queue, project, group, inventory, allowed,
                approval_id=approval_id, legacy_started=None if state.get("track_receipts") else state["started"],
            )
            updated = dict(state, summary=summary)
        queue._save(progress, updated)
        counts = updated["summary"]["counts"]
        advanced = updated.get("next_page") != state.get("next_page") or sum(
            counts[k] for k in ("received", "already_exists")
        ) > sum(previous[k] for k in ("received", "already_exists"))
        return counts["pending"] > 0, advanced


def page_name(page_id):
    if not isinstance(page_id, str) or not re.fullmatch("[a-f0-9]{64}", page_id):
        raise CacheBlocked()
    return "onboarding/pages/" + page_id + ".json"


def save_page(queue, previous, rows):
    page = {"previous": previous, "sources": rows}
    page_id = digest(page)
    name = page_name(page_id)
    saved = queue._load(name)
    if saved is None:
        queue._save(name, page)
    elif saved != page:
        raise CacheBlocked()
    return page_id


def paged_snapshot(provider, queue):
    # Each page commits to its predecessor. The approval binds the entire
    # retained inventory with one digest, independent of its total size.
    head, count, size, rows, statuses = None, 0, 0, [], set()
    with closing(provider.snapshot_rows()) as snapshot:
        for status, row in snapshot:
            check_request()
            statuses.add(status)
            if row is None:
                continue
            row_bytes = len(contract.encode(row)) + 1
            if rows and (len(rows) >= PAGE_ITEMS or size + row_bytes > PAGE_BYTES):
                head = save_page(queue, head, rows)
                size, rows = 0, []
            rows.append(row)
            size += row_bytes
            count += 1
    if rows:
        head = save_page(queue, head, rows)
    return {"status": source_inventory_status(statuses), "head": head, "count": count}


def process_page(sources, harness, client, queue, project, group, inventory, allowed, *,
                 approval_id, state):
    status = inventory["status"]
    page_id = state["next_page"]
    summary = Summary(harness)
    if page_id is None:
        if inventory["count"] != 0:
            raise CacheBlocked()
        return dict(state, summary=summary.result(status))
    page = queue._load(page_name(page_id))
    if page is None or digest(page) != page_id:
        raise CacheBlocked()
    completed = state["completed"]
    later = inventory["count"] - sum(completed["counts"].values()) - len(page["sources"])
    if later < 0 or bool(later) != (page["previous"] is not None):
        raise CacheBlocked()
    portion = process_batch(
        sources, harness, client, queue, project, group, dict(status=status, sources=page["sources"]),
        allowed, approval_id=approval_id, legacy_started=None, exact_event=True,
    )
    for name in summary.counts:
        summary.counts[name] = completed["counts"][name] + portion["counts"][name]
    summary.reasons.update(completed["reasons"])
    summary.reasons.update(portion["reasons"])
    summary.extraction.update(completed["extraction"])
    summary.extraction.update(portion["extraction"])
    if not portion["counts"]["pending"]:
        state = dict(state, next_page=page["previous"], completed=summary.result(status))
    result = summary.result(status)
    result["counts"] = dict(summary.counts, pending=summary.counts["pending"] + later)
    if result["counts"]["pending"]:
        result["status"] = "onboarding-incomplete"
    return dict(state, summary=result)


def read_approval(queue, approval_id, binding):
    if not isinstance(approval_id, str) or not re.fullmatch("[a-f0-9]{64}", approval_id):
        raise CacheBlocked()
    saved = queue._load("onboarding/" + approval_id + ".json")
    if saved is None:
        return None
    if (
        not isinstance(saved, dict)
        or set(saved) not in ({"binding", "inventory"}, {"binding", "inventory", "attempt"})
        or saved["binding"] != binding
        or digest(saved) != approval_id
    ):
        raise CacheBlocked()
    return saved


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
        saved = read_approval(queue, approval_id, binding)
        if saved is None:
            return None
        return approval_id, saved["inventory"]

    snapshot = paged_snapshot(sources.provider, queue)
    saved = {"binding": binding, "inventory": snapshot}
    snapshot_id = digest(saved)
    # Keep one in-flight attempt per exact snapshot, including when the latest
    # approval points to another snapshot. Retain every older approval by ID.
    active = "onboarding/" + snapshot_id + "-active.json"
    approval_id = queue._load(active, snapshot_id)
    prior = read_approval(queue, approval_id, binding)
    if approval_id != snapshot_id and (prior is None or prior["inventory"] != snapshot):
        raise CacheBlocked()
    progress = queue._load(progress_name(approval_id))
    if progress is not None and not batch_pending(progress["summary"]):
        saved["attempt"] = uuid4().hex
        approval_id = digest(saved)
        prior = None
    if prior is None:
        queue._save("onboarding/" + approval_id + ".json", saved)
        if approval_id != snapshot_id:
            queue._save(active, approval_id)
    queue._save("onboarding/latest.json", {"approval_id": approval_id})
    return approval_id, snapshot


def process_batch(sources, harness, client, queue, project, group, inventory, allowed, *,
                  approval_id, legacy_started, exact_event=False):
    summary = Summary(harness)
    wanted = {
        "native_memory_" + digest([project, group, item["source_id"], item["source_fingerprint"]])
        for item in inventory["sources"] if item["status"] == "not-checked"
    }
    existing, other_routes = retained_onboarding_events(queue, group, project, wanted, exact=exact_event)
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
        attempt_file = "onboarding/attempts/" + request_id + ".json"
        retained = existing.get(request_id)
        other = other_routes.get(request_id)
        if retained and retained["state"] == "confirmed" and core_receipt(retained["response"], retained["request"]):
            response = retained["response"]
            recorded = response.get("result", {}).get("replayed") is not True
            owner = queue._load(attempt_file)
            # Older workers left no attempt record. Preserve their in-flight
            # counts when resuming, with an actual validated L0 receipt.
            received = owner == approval_id or (
                owner is None and legacy_started is not None and retained["created"] >= legacy_started
            )
            outcome = "received" if received and recorded else "already_exists"
            summary.add(outcome, response=response)
            continue
        if other:
            if other["state"] == "confirmed":
                summary.add("already_exists", response=other["response"])
            else:
                summary.add("failed", "prior-origin-receipt-unconfirmed")
            continue

        try:
            if not allowed():
                summary.add("pending", "onboarding-origin-changed")
                continue
            body, mtime_ns = sources.read_onboarding_original(
                source_id, fingerprint, locator=item.get("locator"),
            )
            if retained is not None:
                request = retained["request"]
                if body != request["params"]["event"]["content"]:
                    raise CacheBlocked()
            else:
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
                # Persistent host memory can be in a shared or lossy-keyed
                # store. The approved destination is only a delivery route.
                request = l0_request(event, request_id)
            try:
                native_memory_content(body)
            except ValueError:
                summary.add("failed", "original-outside-bound")
                continue
            client.check_event_frame(request)
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
            if not allowed():
                summary.add("pending", "onboarding-origin-changed")
                continue
            # Attribute a later confirmation across worker passes, even when
            # the immutable pending outbox row predates this approval.
            if queue._load(attempt_file) != approval_id:
                queue._save(attempt_file, approval_id)
            options = {"exact_event": True} if exact_event else {}
            response = queue.invoke(request, event_gate=allowed, **options)
        except GlobalRequestTooLarge:
            summary.add("failed", "original-exceeds-transport-envelope")
            continue
        except CollectionCapacityExceeded:
            if exact_event:
                summary.counts["pending"] += len(inventory["sources"]) - index
                summary.reasons["onboarding-queue-capacity"] += 1
                break
            summary.add("failed", "source-or-queue-unavailable")
            continue
        except (CacheBlocked, OSError, ValueError, OverflowError):
            summary.add("failed", "source-or-queue-unavailable")
            continue

        state = response["local_queue"]["state"]
        if state == "confirmed" and core_receipt(
            {field: response[field] for field in ("request_id", "result", "persistence")}, request,
        ):
            outcome = "already_exists" if response["result"].get("replayed") is True else "received"
            summary.add(outcome, response=response)
        elif response.get("local_refresh", {}).get("status") == "event-disabled":
            summary.add("pending", "onboarding-origin-changed")
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


def retained_onboarding_events(queue, group, project, wanted, *, exact=False):
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

    rows = queue._retained_queue_rows(wanted) if exact else queue._queued()
    current = {
        row["request"]["request_id"]: row
        for row in rows
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
