"""Enrollment orchestration inside a trusted device component, not an Agent tool.

Interfaces allow existing HTTPS/SSH and future approved tunnel transports. No
transport or credential store is created implicitly; all secrets stay internal.
"""

import secrets


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


class Enrollment:
    def __init__(self, transport, store, secure_input, profile):
        self.transport, self.store, self.secure_input, self.profile = (
            transport,
            store,
            secure_input,
            profile,
        )

    def run(self):
        # The store must atomically persist credential + nonce before confirm.
        # On restart, reuse it instead of asking for the code a second time.
        try:
            saved = self.store.load()
            if saved is None:
                code = self.secure_input()  # Native secure UI; never stdin/argv/chat.
                if code is None:
                    return {"status": "pairing-cancelled", "installed": False}
                prepared = self.transport.prepare(
                    code, secrets.token_urlsafe(24), self.profile
                )
                saved = {k: prepared[k] for k in ("credential", "nonce", "device_id")}
                self.store.save(saved)
            confirmed = self.transport.confirm(saved["credential"], saved["nonce"])
            if (
                confirmed["phase"] != "active"
                or confirmed["device_id"] != saved["device_id"]
            ):
                raise ValueError("Invalid device confirmation")
            return {
                "status": "device-paired",
                "device_id": confirmed["device_id"],
                "grant": confirmed["grant"],
                "installed": False,
                "next_action": "Run the owned native installation plan and protocol check; pairing alone is not installation.",
            }
        except EnrollmentTerminal as error:
            return {
                "status": "pairing-reenrollment-review-required",
                "reason": error.reason,
                "installed": False,
                "next_action": "Use the trusted component UI to confirm clearing only this exact terminal record, then obtain a fresh code; preserve active or unknown entries.",
            }
        except Exception:
            return {
                "status": "pairing-resume-required",
                "installed": False,
                "next_action": "Preserve private component state and resume. No credential or raw transport error is returned.",
            }

    def reset_terminal(self, confirm_reset):
        try:
            saved = self.store.load()
            if saved is None:
                return {"status": "pairing-not-enrolled", "installed": False}
            try:
                result = self.transport.confirm(saved["credential"], saved["nonce"])
                if (
                    result.get("phase") != "active"
                    or result.get("device_id") != saved["device_id"]
                ):
                    raise ValueError("Unknown confirmation")
                return {"status": "active-enrollment-preserved", "installed": False}
            except EnrollmentTerminal as error:
                if not confirm_reset(error.reason, saved["device_id"]):
                    return {"status": "pairing-reset-cancelled", "installed": False}
                # Store clears only the exact observed record; a changed/new
                # registration must survive. Native uses its persistent ref.
                self.store.clear(saved)
                return {"status": "pairing-reenrollment-ready", "installed": False}
        except Exception:
            return {"status": "pairing-reset-blocked", "installed": False}
