"""application / installation / follow_up: one owner for each contract and rule."""

ONBOARD_COVERAGE_GUIDANCE = (
    "Once host MCP tools are available, follow the public Onboarding guide at https://github.com/goodname427/agent-brain-client/blob/main/BOOTSTRAP.md#onboarding for connection verification, authorized source review, CAS writes and coverage reporting. "
    "The guide can be used with an empty Agent Brain account and without preloaded Skills. This handoff records that no source scan or data transfer has occurred. "
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
