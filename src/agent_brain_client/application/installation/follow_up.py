"""application / installation / follow_up: one owner for each contract and rule."""

ONBOARD_COVERAGE_GUIDANCE = (
    "After installation or reinstallation, once host tools are available, use the currently loaded agent-brain-onboard Skill for source discovery, comparison, approved writes and coverage reporting. "
    "This Client handoff records that no source scan or data transfer has occurred; installation, identity and receipt state do not replace the Skill result. "
)


def installation_follow_up(result):
    """Pure Agent handoff; no source discovery, credentials, model task or upload.

    A prior coverage result is deliberately not inferred from installation or
    identity metadata. The Agent must obtain fresh authorized source evidence.
    """
    value = dict(result)
    if "model_loaded" in value:
        value["model_loaded"] = "unknown"
    if value.get("installed") is True:
        value["onboard_check"] = {
            "status": "coverage-check-required",
            "coverage": "unknown",
            "account_complete": False,
            "source_scan_performed": False,
            "harness_source_inventory_required": True,
            "agentbrain_catalog_is_source_inventory": False,
            "data_transferred": False,
            "next_action": ONBOARD_COVERAGE_GUIDANCE,
        }
    return value
