"""Content-free recall evidence, separate from cached memory content."""

import math
import re
from agent_brain_client.adapters.storage.collection import settings_path
from agent_brain_client.adapters.storage import metadata as metadata
from agent_brain_client.adapters.platforms.filesystem import private_directory


def save_recall_observation(value, receipt):
    directory = settings_path(value["device_home"]).parent
    receipts = directory / "recall-receipts"
    private_directory(receipts)
    metadata.write(receipts / (receipt["request_key"] + ".json"), receipt)
    metadata.write(directory / "last-recall.json", receipt)


def recall_observation(value, request_key=None):
    """Read only the content-free receipt, never a cached memory payload."""
    directory = settings_path(value["device_home"]).parent
    if request_key is not None and not re.fullmatch("[a-f0-9]{64}", request_key):
        raise ValueError("Exact recall request key required")
    file = (
        directory / "recall-receipts" / (request_key + ".json")
        if request_key
        else directory / "last-recall.json"
    )
    if not file.exists():
        return {"status": "unobserved", "model_used": "unknown"}
    saved = metadata.private_json(file)
    if saved.get("schema") == 2:
        fields = {
            "schema",
            "at",
            "stage",
            "request_key",
            "request_id",
            "session_sha256",
            "turn_sha256",
            "query_sha256",
            "output_written",
            "model_used",
            "status",
            "error_type",
            "layer_counts",
            "item_count",
            "context_bytes",
            "context_sha256",
            "offline_cache",
            "retrieval",
        }
        counts = saved.get("layer_counts", {})
        retrieval = saved.get("retrieval", {})
        if (
            set(saved) != fields
            or saved.get("stage") != "agentbrain-global-recall"
            or saved.get("model_used") != "unknown"
            or saved.get("status") not in ("context-prepared", "empty", "failed")
            or any(
                not isinstance(saved.get(k), str)
                or not re.fullmatch("[a-f0-9]{64}", saved[k])
                for k in (
                    "request_key",
                    "session_sha256",
                    "turn_sha256",
                    "query_sha256",
                )
            )
            or saved.get("request_id") != "global_recall_" + saved["request_key"]
            or request_key is not None
            and saved["request_key"] != request_key
            or type(saved.get("output_written")) is not bool
            or type(saved.get("offline_cache")) is not bool
            or not isinstance(counts, dict)
            or set(counts) != {"L1", "L2", "L3"}
            or any(type(n) is not int or n < 0 for n in counts.values())
            or type(saved.get("item_count")) is not int
            or saved["item_count"] != sum(counts.values())
            or type(saved.get("context_bytes")) is not int
            or not 0 <= saved["context_bytes"] <= 24576
            or type(saved.get("at")) not in (int, float)
            or not math.isfinite(saved["at"])
            or saved["context_sha256"] is not None
            and not re.fullmatch("[a-f0-9]{64}", saved["context_sha256"])
            or saved["error_type"] is not None
            and not re.fullmatch("[A-Za-z][A-Za-z0-9_]{0,63}", saved["error_type"])
            or not isinstance(retrieval, dict)
            or set(retrieval)
            != {"candidate_count", "returned", "selected", "omitted_for_budget"}
            or any(
                type(retrieval.get(k)) is not int or retrieval[k] < 0
                for k in ("candidate_count", "omitted_for_budget")
            )
            or not isinstance(retrieval.get("selected"), list)
            or not isinstance(retrieval.get("returned"), list)
            or any(
                not isinstance(ref, dict)
                or set(ref) != {"layer", "record_sha256", "content_sha256"}
                or ref["layer"] not in counts
                or any(
                    not isinstance(ref[k], str)
                    or not re.fullmatch("[a-f0-9]{64}", ref[k])
                    for k in ("record_sha256", "content_sha256")
                )
                for ref in [*retrieval["selected"], *retrieval["returned"]]
            )
        ):
            raise ValueError("Invalid request recall receipt")
        return saved
    fields = {
        "schema",
        "at",
        "stage",
        "status",
        "layer_counts",
        "item_count",
        "context_bytes",
        "offline_cache",
        "model_used",
    }
    counts = saved.get("layer_counts", {})
    if (
        set(saved) != fields
        or saved["schema"] != 1
        or saved["stage"] != "agentbrain-global-recall"
        or saved["status"] not in ("context-prepared", "empty")
        or saved["model_used"] != "unknown"
        or not isinstance(counts, dict)
        or set(counts) != {"L1", "L2", "L3"}
        or any(type(n) is not int or not 0 <= n <= 3 for n in counts.values())
        or type(saved["item_count"]) is not int
        or saved["item_count"] != sum(counts.values())
        or saved["item_count"] > 3
        or type(saved["context_bytes"]) is not int
        or not 0 <= saved["context_bytes"] <= 131072
        or type(saved["offline_cache"]) is not bool
        or type(saved["at"]) not in (int, float)
        or not math.isfinite(saved["at"])
    ):
        raise ValueError("Invalid content-free recall receipt")
    return saved
