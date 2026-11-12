"""Current-question recall and receipts shared by all native Harnesses."""

import time
from agent_brain_client.domain import privacy
from agent_brain_client.domain.scopes import digest, restricted
from agent_brain_client.application.memory.context import render_context


def recall_prompt(value, local, payload, query, *, services, observe=None):
    question = query
    if (
        not isinstance(question, str)
        or not question.strip()
        or privacy.private_content(question)
        or restricted(question)
    ):
        return []

    directory = services.settings_path(value["device_home"]).parent
    key = digest(
        [
            digest(value["scope"]),
            local,
            payload.get("session_id"),
            payload.get("turn_id") or payload.get("generation_id"),
            question,
            *([value["recall"]] if value.get("recall") else []),
        ]
    )
    stamp = directory / ("prompt-" + key + ".json")
    with services.locked(directory / ("prompt-recall-" + key + ".lock")):
        if services.cache_exists(stamp):
            return []
        query = question[:1024]
        receipt = {
            "schema": 2,
            "at": time.time(),
            "stage": "agentbrain-global-recall",
            "request_key": key,
            "request_id": "global_recall_" + key,
            "session_sha256": digest(payload["session_id"]),
            "turn_sha256": digest(payload.get("turn_id") or payload.get("generation_id") or payload["session_id"]),
            "query_sha256": digest(query),
            "output_written": False,
            "model_used": "unknown",
            "status": "failed",
            "error_type": None,
            "layer_counts": {"L1": 0, "L2": 0, "L3": 0},
            "item_count": 0,
            "context_bytes": 0,
            "context_sha256": None,
            "offline_cache": False,
            "retrieval": {
                "candidate_count": 0,
                "returned": [],
                "selected": [],
                "omitted_for_budget": 0,
            },
        }
        try:
            with services.operation_stage("global-recall"):
                items = services.recall(
                    value,
                    local,
                    payload["session_id"],
                    query,
                    request_id=receipt["request_id"],
                    observe=receipt["retrieval"],
                )
            result = items
        except Exception as error:
            receipt["error_type"] = type(error).__name__
            services.save_observation(value, receipt)
            if observe is not None:
                observe(receipt)
            raise
        context = render_context(items) if items else ""
        receipt.update(
            {
                "status": "context-prepared" if result else "empty",
                "layer_counts": {
                    layer: sum(item.get("layer") == layer for item in items)
                    for layer in ("L1", "L2", "L3")
                },
                "item_count": len(items),
                "context_bytes": len(context.encode()),
                "context_sha256": digest(context),
                "offline_cache": any(
                    item.get("offline_cache") is True for item in items
                ),
            }
        )
        services.save_observation(value, receipt)
        services.write(stamp, {"at": time.time()})
    if observe is not None:
        observe(receipt)
    return result
