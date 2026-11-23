"""application / installation / follow_up: one owner for each contract and rule."""

from agent_brain_client.application.memory.guidance import ONBOARDING_GUIDANCE


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
            "data_transferred": False,
            "next_action": ONBOARDING_GUIDANCE,
        }
    return value
