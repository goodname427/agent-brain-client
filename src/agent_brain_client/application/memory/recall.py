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
from agent_brain_client.application.memory.context import select_context_items
from agent_brain_client.application.control import recall_timing_stage
import re


def _fit_context(items, references, query, observe, *, cached=False):
    selected = select_context_items(
        items, references, query, cached=cached, automatic=observe is not None
    )
    returned = [
        {key: ref[key] for key in ("layer", "record_sha256", "content_sha256")}
        for ref in references
    ]
    refs = []
    for index, item in selected.items():
        if index >= len(returned):
            continue  # Legacy private caches did not record the original ID.
        ref = returned[index]
        if "presentation" in item:
            ref = dict(
                ref,
                presentation=item["presentation"],
                output_content_sha256=digest(item["content"]),
            )
            if "index_metadata" in item:
                ref["index_metadata_sha256"] = digest(item["index_metadata"])
        refs.append(ref)
    if observe is not None:
        observe.update(
            candidate_count=len(items),
            returned=returned,
            selected=refs,
            omitted_for_budget=len(items) - len(selected),
        )
    return list(selected.values()), refs


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
    scope = value["scope"]
    selected = select(scope, local, recall=True)
    if selected is None:
        raise ValueError("Origin not approved for recall")
    client = services.client(value, local)
    options = dict(value.get("recall", {}))
    params = dict(
        client.params(session, recall=True), query=query, limit=options.pop("maxResults", 5)
    )
    params["recall"] = options
    cache_query = digest([query, params["limit"], options])
    automatic = observe is not None
    cache = services.settings_path(value["device_home"]).parent / (
        "recall-" + digest(local) + ".json"
    )
    with recall_timing_stage("wire"):
        response = client.wire(
            "memory_query", params, **({"rid": request_id} if request_id else {})
        )
    if "error" in response:
        error = response["error"]
        if (
            error.get("code") == "transport_unconfirmed"
            and error.get("retryable") is True
            and services.cache_exists(cache)
        ):
            saved = services.read_cache(cache)
            if (
                saved.get("scope_sha256") == digest(value["scope"])
                and saved.get("query_sha256") == cache_query
                and ("evidence_selected" not in saved or saved["evidence_selected"] is automatic)
                and services.approved(value)
            ):
                items = [
                    dict(item, offline_cache=True, remote_confirmation=False)
                    for item in saved["items"]
                ]
                if not scope["recall_personal"] and any(
                    item.get("scope") == "personal" for item in items
                ):
                    raise ValueError("Cached personal recall is disabled")
                cached_evidence = saved.get("evidence_selected") is True
                references = saved.get("selected", [])
                for index, item in enumerate(items):
                    if "presentation" not in item:
                        continue
                    ref = references[index] if index < len(references) else {}
                    index_digest = (
                        digest(item["index_metadata"]) if "index_metadata" in item else None
                    )
                    if (
                        not cached_evidence
                        or ref.get("layer") != item["layer"]
                        or ref.get("record_sha256") != item["record_sha256"]
                        or ref.get("presentation") != item["presentation"]
                        or ref.get("output_content_sha256") != digest(item["content"])
                        or ref.get("index_metadata_sha256") != index_digest
                    ):
                        raise ValueError("Cached recall excerpt changed")
                selected, _ = _fit_context(
                    items, references, query, observe, cached=cached_evidence
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
    if observe is not None and response.get("recall_partial") is True:
        observe.update(partial=True, server_budget_trimmed=True)
    ids = {}
    if scope["recall_personal"]:
        ids[namespace(client.actor, scope["profile_id"])] = "personal"
    if selected["sharing"] == "project":
        ids[namespace(client.actor, source_id(scope, local, "project"))] = "project"
    items = []
    references = []
    invalid_group = None
    for group in data["namespace_results"]:
        try:
            group_items, group_refs = _verified_group(
                group, value, local, session, client, ids
            )
        except (ValueError, TypeError) as error:
            invalid_group = error
            continue
        items.extend(group_items)
        references.extend(group_refs)
    if invalid_group is not None:
        if observe is not None:
            observe.update(partial=True, source_verification_failed=True)
        if not items:
            raise ValueError("No usable verified recall source") from invalid_group
    items, refs = _fit_context(items, references, query, observe)
    if invalid_group is None:  # Source verification failures preserve the previous snapshot.
        try:
            services.write_cache(
                cache,
                {
                    "scope_sha256": digest(value["scope"]),
                    "query_sha256": cache_query,
                    "items": items,
                    "selected": refs,
                    "evidence_selected": automatic,
                },
            )
        except (OSError, ValueError):
            pass
    return items


def _verified_group(group, value, local, session, client, ids):
    from agent_brain_client.domain.contract import core_recall, verified_memory_sources

    items, references = [], []
    if (
        not isinstance(group, dict)
        or set(group)
        not in (
            {"project_id", "sharing", "recall"},
            {"project_id", "sharing", "recall", "source_device_id"},
        )
        or ids.get(group["project_id"]) != group["sharing"]
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
            if not isinstance(row["content"], str):
                raise ValueError("Recall text required")
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
                    (select(value["scope"], x) or {}).get("sharing") != group["sharing"]
                    for x in context["origins"]
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
            if layer == "L2" and isinstance(row.get("path"), str):
                # Keep the authorized logical path transient. Only a
                # screened leaf name may appear in a projected index.
                references[-1]["scenario_path"] = row["path"]
    return items, references
