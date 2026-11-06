"""Enrollment orchestration inside a trusted device component, not an Agent tool.

Interfaces allow existing HTTPS/SSH and future approved tunnel transports. No
transport or credential store is created implicitly; all secrets stay internal.
"""


def reviewed_ids(types, ids):
    from agent_brain_client.domain.contract import grant_ids

    grant_ids(types, ids)


def allowed_id(ids, kind, identity):
    from agent_brain_client.domain.contract import allows

    return allows(ids, kind, identity)


class EnrollmentTerminal(Exception):
    def __init__(self, reason):
        if reason not in (
            "pairing_expired",
            "device_revoked",
            "pairing_prepare_rejected",
        ):
            raise ValueError("Not a terminal enrollment")
        self.reason = reason
        super().__init__(reason)
