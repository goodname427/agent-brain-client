"""domain / receipts: one owner for each contract and rule."""


def core_receipt(response, request):
    """Acceptance and a durable receipt ledger are not Core/Git durability."""
    if (
        not isinstance(response, dict)
        or set(response) != {"request_id", "result", "persistence"}
        or response.get("request_id") != request["request_id"]
    ):
        return False
    receipt = response.get("persistence")
    value = response.get("result")
    if (
        not isinstance(receipt, dict)
        or set(receipt)
        != {"state", "durable", "github_confirmed", "receipt_ledger_durable"}
        or receipt
        != {
            "state": "core-accepted",
            "durable": False,
            "github_confirmed": False,
            "receipt_ledger_durable": True,
        }
    ):
        return False
    if any(
        type(receipt[name]) is not bool
        for name in ("durable", "github_confirmed", "receipt_ledger_durable")
    ):
        return False
    fields = {
        "event_id",
        "state",
        "message_ids",
        "core_acceptance",
        "ledger_sync",
        "pipeline_state",
        "model_extraction_verified",
    }
    optional = {"replayed", "capture_extraction"}
    if (
        not isinstance(value, dict)
        or not fields <= set(value)
        or set(value) - fields - optional
    ):
        return False
    # Capture diagnosis is additive metadata, not event acceptance or durability
    # proof. Preserve it without coupling the receipt to extraction states: a
    # pending/no-facts/review-required extraction can still have a committed L0.
    if "capture_extraction" in value and not isinstance(
        value["capture_extraction"], dict
    ):
        return False
    if "replayed" in value and type(value["replayed"]) is not bool:
        return False
    ids = value["message_ids"]
    return (
        value["event_id"] == request["params"]["id"]
        and value["state"] == "committed"
        and value["core_acceptance"] == "acknowledged-not-durability-proof"
        and value["ledger_sync"] == "sqlite-wal-full"
        and value["pipeline_state"] == "unverified"
        and value["model_extraction_verified"] is False
        and isinstance(ids, list)
        and len(ids) == len(request["params"]["event"]["messages"])
        and all(isinstance(mid, str) and 0 < len(mid) <= 256 for mid in ids)
        and len(set(ids)) == len(ids)
    )
