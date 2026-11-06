"""Profile-bound persisted enrollment record contract, shared by use cases and requests."""

import re
from agent_brain_client.domain.authorization import valid_secret


def enrollment_record(saved, profile_sha, grant):
    basic = {"schema", "profile_sha256", "phase", "nonce"}
    if (
        not isinstance(saved, dict)
        or type(saved.get("schema")) is not int
        or saved["schema"] != 1
        or saved.get("profile_sha256") != profile_sha
        or not valid_secret(saved.get("nonce"))
    ):
        raise ValueError("Enrollment record binding changed")
    if saved.get("phase") == "preparing":
        if set(saved) != basic | {"code"} or not valid_secret(saved["code"]):
            raise ValueError("Invalid prepare recovery")
    elif saved.get("phase") in ("pending", "active"):
        if (
            set(saved) != basic | {"credential", "device_id", "grant"}
            or not valid_secret(saved["credential"])
            or not re.fullmatch(r"[a-f0-9]{32}", saved.get("device_id", ""))
            or saved["grant"] != grant
        ):
            raise ValueError("Invalid device recovery")
    else:
        raise ValueError("Unknown enrollment preserved")
    return saved
