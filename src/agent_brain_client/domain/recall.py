"""domain / recall: one owner for each contract and rule."""

import hashlib
import math
from pathlib import PurePosixPath, PureWindowsPath
import re
import unicodedata
from agent_brain_client.domain.validation import encode
from agent_brain_client.domain.events import event_identifier
from agent_brain_client.domain.privacy import private_recall_text


def recall_options(value):
    if not isinstance(value, dict) or set(value) - {
        "strategy",
        "scoreThreshold",
        "maxCharsPerMemory",
        "maxTotalRecallChars",
        "timeoutMs",
    }:
        raise ValueError("Native recall options required")
    for key, number in value.items():
        if key == "strategy":
            if number not in ("keyword", "embedding", "hybrid"):
                raise ValueError("Native recall strategy required")
        elif key == "scoreThreshold":
            if type(number) not in (int, float) or not math.isfinite(number):
                raise ValueError("Native relevance threshold required")
        elif (
            type(number) is not int
            or not (1 if key == "timeoutMs" else 0) <= number <= 2**31 - 1
        ):
            raise ValueError("Native recall budget required")
    return value


def core_recall(value, context):
    if (
        not isinstance(value, dict)
        or set(value) != {"content_api_version", "method", "source", "scope", "recall"}
        or value["content_api_version"] != 1
        or value["method"] != "core-recall"
        or value["source"] != "core"
        or value["scope"] != context
    ):
        raise ValueError("Unverified Core recall scope")
    recall = value["recall"]
    if (
        not isinstance(recall, dict)
        or set(recall)
        != {"L0", "L1", "L2", "L3", "cross_session", "model_extraction_verified"}
        or recall["cross_session"] is not True
        or recall["model_extraction_verified"] is not False
        or len(encode(recall)) > 240000
    ):
        raise ValueError("Bounded Core recall required")
    for name in ("L0", "L1", "L2"):
        if not isinstance(recall[name], list) or any(
            not isinstance(row, dict) or not isinstance(row.get("provenance"), dict)
            for row in recall[name]
        ):
            raise ValueError("Bounded sourced Core rows required")
    if (
        not isinstance(recall["L3"], dict)
        or not isinstance(recall["L3"].get("provenance"), dict)
        or recall["L3"].get("content") is not None
        and not isinstance(recall["L3"].get("content"), str)
    ):
        raise ValueError("Sourced Core profile required")


def recall_origin(value):
    """Metadata only; no filesystem discovery or new source authorization."""

    def path(raw):
        if not isinstance(raw, str) or any(c in raw for c in ("\x00", "\r", "\n")):
            raise ValueError("Absolute origin required")
        raw = unicodedata.normalize("NFC", raw)
        windows = PureWindowsPath(raw)
        if windows.drive:
            if (
                not re.match(r"^[A-Za-z]:[\\/]", raw)
                or not windows.is_absolute()
                or ".." in windows.parts
                or any(":" in p for p in windows.parts[1:])
            ):
                raise ValueError("Local drive origin required")
            return windows.as_posix().casefold()
        if (
            not raw.startswith("/")
            or raw.startswith("//")
            or "\\" in raw
            or ".." in PurePosixPath(raw).parts
        ):
            raise ValueError("Absolute origin required")
        return str(PurePosixPath(raw)).casefold()

    if (
        not isinstance(value, dict)
        or set(value) != {"kind", "cwd", "project_root", "repository_root"}
        or value["kind"] not in ("project", "non-project")
    ):
        raise ValueError("Exact source origin required")
    cwd = path(value["cwd"])
    if value["kind"] == "project":
        root = path(value["project_root"])
        path(value["repository_root"])
        if cwd != root and not cwd.startswith(root.rstrip("/") + "/"):
            raise ValueError("Working directory outside project")
    elif value["project_root"] is not None or value["repository_root"] is not None:
        raise ValueError("Non-project origin cannot claim a repository")
    return value["kind"], path(value["repository_root"] or value["cwd"])


def global_recall_params(params):
    if not isinstance(params, dict) or set(params) - {"recall"} != {
        "origin",
        "session_id",
        "query",
        "limit",
    }:
        raise ValueError("Exact layered recall request required")
    recall_options(params.get("recall", {}))
    recall_origin(params["origin"])
    event_identifier(params["session_id"])
    if (
        not isinstance(params["query"], str)
        or not params["query"].strip()
        or len(params["query"]) > 1024
        or type(params["limit"]) is not int
        or not 1 <= params["limit"] <= 2**31 - 1
    ):
        raise ValueError("Bounded recall query required")
    if private_recall_text(params["query"]):
        raise ValueError("Private recall text excluded")


def verified_memory_sources(proof, device, project, direct_l1=False):
    sources = proof.get("sources", [])
    manual = proof.get("manual_sources", [])
    if (
        proof.get("status")
        not in ("verified-native-generation-graph", "verified-l1-source-graph")
        or not proof.get("generation_ids")
        and not (direct_l1 and manual)
        or not sources
        and not manual
        or not isinstance(sources, list)
        or not isinstance(manual, list)
        or len(sources) + len(manual) > 100
        or set(proof.get("event_ids", [])) != {s.get("event_id") for s in sources}
    ):
        return False
    for source in sources:
        if (
            not isinstance(source, dict)
            or source.get("state") != "committed"
            or source.get("identity", {}).get("device_id") != device
            or source.get("identity", {}).get("project_id") != project
        ):
            return False
    for source in manual:
        if (
            proof.get("status") != "verified-l1-source-graph"
            or source.get("kind") not in ("agent", "legacy-import")
            or source.get("state") != "committed"
            or source.get("identity", {}).get("project_id") != project
            or source.get("identity", {}).get("device_id") != source.get("device_id")
            or not re.fullmatch("abm-[a-f0-9]{64}", str(source.get("record_id")))
            or not re.fullmatch("[a-f0-9]{64}", str(source.get("revision")))
            or not re.fullmatch(
                "[a-z0-9][a-z0-9-]{0,63}", str(source.get("original_id"))
            )
            or not isinstance(source.get("actor"), str)
            or not source["actor"]
            or not isinstance(source.get("device_id"), str)
            or not source["device_id"]
        ):
            return False
        try:
            if source.get("origin") is not None:
                recall_origin(source["origin"])
        except (ValueError, TypeError):
            return False
        if source["kind"] == "legacy-import" and (
            not re.fullmatch("[a-f0-9]{40}", str(source.get("commit")))
            or not re.fullmatch("[a-f0-9]{64}", str(source.get("plan_sha256")))
        ):
            return False
    return True


def global_recall_result(value, params, device):
    """Keep layer, committed event proof and source applicability intact."""
    global_recall_params(params)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "namespace_results"}
        or value["schema"] != 1
        or not isinstance(value["namespace_results"], list)
        or len(value["namespace_results"]) > 18
        or len(encode(value)) > 240000
    ):
        raise ValueError("Bounded layered response required")
    current = recall_origin(params["origin"])
    for group in value["namespace_results"]:
        if (
            not isinstance(group, dict)
            or set(group)
            not in (
                {"project_id", "sharing", "recall"},
                {"project_id", "sharing", "recall", "source_device_id"},
            )
            or group["sharing"] not in ("personal", "project")
            or not re.fullmatch(r"abp-[a-f0-9]{32}", str(group["project_id"]))
        ):
            raise ValueError("Verified namespace required")
        source_device = group.get("source_device_id", device)
        if (
            not isinstance(source_device, str)
            or not re.fullmatch(r"[a-f0-9]{32}", source_device)
            or source_device != device
            and group["sharing"] != "personal"
        ):
            raise ValueError("Approved personal peer required")
        context = {
            "project_id": group["project_id"],
            "session_id": params["session_id"],
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
        if found["L0"]:
            raise ValueError("Derived-only recall required")
        for row in [*found["L1"], *found["L2"], found["L3"]]:
            if row.get("content") is None:
                continue
            if not isinstance(row["content"], str):
                raise ValueError("Recall text required")
            if private_recall_text(row["content"]):
                raise ValueError("Private recall text excluded")
            proof = row["provenance"]
            if not verified_memory_sources(
                proof, source_device, group["project_id"], row in found["L1"]
            ):
                raise ValueError("Committed L1 source provenance required")
            source = row.get("source_context", {})
            if (
                not isinstance(source, dict)
                or set(source) != {"origins", "relation", "use", "global_rule"}
                or source["use"] != "reference-only"
                or source["global_rule"] is not False
                or not isinstance(source["origins"], list)
                or len(source["origins"]) > 32
                or not source["origins"]
                and not proof.get("manual_sources")
            ):
                raise ValueError("Source applicability required")
            matches = [recall_origin(origin) == current for origin in source["origins"]]
            relation = (
                "unknown-source"
                if not matches
                else "current-source"
                if all(matches)
                else "other-source"
                if not any(matches)
                else "mixed-source"
            )
            if source["relation"] != relation:
                raise ValueError("Source relation changed")
        if len(found["L1"]) > params["limit"]:
            raise ValueError("L1 exceeds native namespace limit")


def validate_capture_selection(value):
    """Exact two committed capture batches, filled from runtime receipts only."""
    if not isinstance(value, dict) or set(value) != {"source_session_id", "batches"}:
        raise ValueError("Exact capture selection required")
    identifier = value["source_session_id"]
    if not isinstance(identifier, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", identifier
    ):
        raise ValueError("Source session required")
    batches = value["batches"]
    if not isinstance(batches, list) or len(batches) != 2:
        raise ValueError("Exactly two receipted batches required")
    mids = []
    events = []
    keys = []
    for batch in batches:
        if not isinstance(batch, dict) or set(batch) != {
            "batch_key",
            "event_id",
            "payload_sha256",
            "message_ids",
            "messages_sha256",
        }:
            raise ValueError("Exact batch receipt required")
        for field in ("batch_key", "payload_sha256", "messages_sha256"):
            if not isinstance(batch[field], str) or not re.fullmatch(
                r"[a-f0-9]{64}", batch[field]
            ):
                raise ValueError("Runtime digest required")
        event = batch["event_id"]
        ids = batch["message_ids"]
        if not isinstance(event, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", event
        ):
            raise ValueError("Runtime event required")
        if (
            not isinstance(ids, list)
            or len(ids) != 2
            or any(
                not isinstance(i, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", i)
                for i in ids
            )
        ):
            raise ValueError("Two runtime message IDs required")
        mids += ids
        events.append(event)
        keys.append(batch["batch_key"])
    if len(set(mids)) != 4 or len(set(events)) != 2 or len(set(keys)) != 2:
        raise ValueError("Distinct incremental batches required")
    return value


def exact_capture_response(recall, params, device_id):
    selection = params["selection"]
    events = {b["event_id"]: b["payload_sha256"] for b in selection["batches"]}
    selection_sha = hashlib.sha256(encode(selection)).hexdigest()
    if recall["L0"] or len(recall["L1"]) != 1 or len(recall["L2"]) != 1:
        raise ValueError("Exactly one row per layer required")
    for layer, row in [
        ("L1", recall["L1"][0]),
        ("L2", recall["L2"][0]),
        ("L3", recall["L3"]),
    ]:
        p = row["provenance"]
        sources = p.get("sources", [])
        ids = p.get("event_ids", [])
        content = row.get("content")
        if (
            not isinstance(content, str)
            or not content
            or p.get("selection_sha256") != selection_sha
            or p.get("content_sha256") != hashlib.sha256(content.encode()).hexdigest()
            or p.get("status") != "verified-native-generation-graph"
            or p.get("model_output_truth_verified") is not False
            or not p.get("generation_ids")
            or not ids
            or not set(ids) <= set(events)
            or len(sources) != len(set(ids))
            or {s.get("event_id") for s in sources} != set(ids)
            or any(
                s.get("state") != "committed"
                or s.get("payload_sha256") != events.get(s.get("event_id"))
                or s.get("identity", {}).get("device_id") != device_id
                or s.get("identity", {}).get("project_id")
                != params["context"]["project_id"]
                or s.get("identity", {}).get("session_id")
                != selection["source_session_id"]
                for s in sources
            )
            or layer == "L3"
            and set(ids) != set(events)
        ):
            raise ValueError("Unverified exact capture response")
