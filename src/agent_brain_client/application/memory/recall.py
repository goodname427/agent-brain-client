"""Recall shared L1/L2/L3 memory through injected service and cache ports."""

from agent_brain_client.domain.scopes import (
    digest,
    select,
    namespace,
    source_id,
    source_context,
    restricted,
)
from agent_brain_client.domain import privacy as privacy
from agent_brain_client.application.memory.context import bounded_context_items
import re


def recall(
    value,
    local,
    session,
    query="personal preferences and task progress",
    *,
    services,
    request_id=None,
    observe=None,
):
    client = services.client(value, local)
    options = dict(value.get("recall", {}))
    params = dict(
        client.params(session), query=query, limit=options.pop("maxResults", 5)
    )
    params["recall"] = options
    cache_query = digest([query, params["limit"], options])
    response = client.wire(
        "memory_query", params, **({"rid": request_id} if request_id else {})
    )
    if "error" in response:
        cache = services.settings_path(value["device_home"]).parent / (
            "recall-" + digest(local) + ".json"
        )
        if response["error"].get(
            "code"
        ) == "transport_unconfirmed" and services.cache_exists(cache):
            saved = services.read_cache(cache)
            if (
                saved.get("scope_sha256") == digest(value["scope"])
                and saved.get("query_sha256") == cache_query
                and services.approved(value)
                and select(value["scope"], local) is not None
            ):
                items = [
                    dict(item, offline_cache=True, remote_confirmation=False)
                    for item in saved["items"]
                ]
                selected = bounded_context_items(items)
                refs = [
                    ref
                    for item, ref in zip(items, saved.get("selected", []))
                    if any(item is kept for kept in selected)
                ]
                if observe is not None:
                    observe.update(
                        candidate_count=len(items),
                        returned=saved.get("selected", []),
                        selected=refs,
                        omitted_for_budget=len(items) - len(selected),
                    )
                return selected
        raise ValueError("Global recall unavailable")
    data = response["result"]
    # A pool has at most 17 enrolled devices: two local namespaces plus one
    # personal namespace for each of the 16 peers. Empty groups still count.
    if (
        set(data) != {"schema", "namespace_results"}
        or data["schema"] != 1
        or not isinstance(data["namespace_results"], list)
        or len(data["namespace_results"]) > 18
    ):
        raise ValueError("Bounded global recall required")
    from agent_brain_client.domain.contract import core_recall, verified_memory_sources

    selected = select(value["scope"], local)
    ids = {
        namespace(client.actor, value["scope"]["profile_id"]),
        namespace(client.actor, source_id(value["scope"], local, selected["sharing"])),
    }
    items = []
    references = []
    for group in data["namespace_results"]:
        if (
            set(group)
            not in (
                {"project_id", "sharing", "recall"},
                {"project_id", "sharing", "recall", "source_device_id"},
            )
            or group["project_id"] not in ids
            or group["sharing"] not in ("personal", "project")
        ):
            raise ValueError("Global recall namespace changed")
        device = group.get("source_device_id", client.device)
        if not isinstance(device, str) or not re.fullmatch("[a-f0-9]{32}", device):
            raise ValueError("Global recall source device changed")
        peer = device != client.device
        if peer and (
            group["sharing"] != "personal"
            or group["project_id"]
            != namespace(client.actor, value["scope"]["profile_id"])
            or not value["scope"]["recall_personal"]
        ):
            raise ValueError("Only the approved personal pool can return peer memory")
        context = {
            "project_id": group["project_id"],
            "session_id": session,
            "cross_session": True,
        }
        core_recall(
            {
                "content_api_version": 1,
                "method": "core-recall",
                "source": "core",
                "scope": context,
                "recall": group["recall"],
            },
            context,
        )
        found = group["recall"]
        if found["L0"] != []:
            raise ValueError("Derived-only global recall required")
        for layer in ("L1", "L2", "L3"):
            for row in [found[layer]] if layer == "L3" else found[layer]:
                if row.get("content") is None:
                    continue
                proof = row.get("provenance", {})
                if not verified_memory_sources(
                    proof, device, group["project_id"], layer == "L1"
                ):
                    raise ValueError("Global recall provenance changed")
                context = row.get("source_context")
                # The authenticated Server checks a peer's own approved origins.
                # Client paths describe this device and cannot authorize a peer's
                # filesystem. Retain structural/applicability checks for both.
                if (
                    not isinstance(context, dict)
                    or source_context(context.get("origins", []), local) != context
                    or not peer
                    and not proof.get("manual_sources")
                    and any(
                        select(value["scope"], x) is None for x in context["origins"]
                    )
                ):
                    raise ValueError("Verified origin applicability required")
                if privacy.private_content(row["content"]) or restricted(
                    row["content"]
                ):
                    continue
                items.append(
                    {
                        "scope": group["sharing"],
                        "layer": layer,
                        "content": row["content"],
                        "source_context": context,
                        "relevance": "native-query-match"
                        if layer == "L1"
                        else "assess-against-current-question",
                    }
                )
                references.append(
                    {
                        "layer": layer,
                        "record_sha256": digest(
                            row.get("id") or row.get("path") or "persona.md"
                        ),
                        "content_sha256": digest(row["content"]),
                    }
                )
    selected = bounded_context_items(items)
    refs = [
        ref
        for item, ref in zip(items, references)
        if any(item is kept for kept in selected)
    ]
    if observe is not None:
        observe.update(
            candidate_count=len(items),
            returned=references,
            selected=refs,
            omitted_for_budget=len(items) - len(selected),
        )
    items = selected
    cache = services.settings_path(value["device_home"]).parent / (
        "recall-" + digest(local) + ".json"
    )
    services.write_cache(
        cache,
        {
            "scope_sha256": digest(value["scope"]),
            "query_sha256": cache_query,
            "items": items,
            "selected": refs,
        },
    )
    return items
