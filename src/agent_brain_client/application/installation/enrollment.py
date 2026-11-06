"""Durable device enrollment use case; credential and HTTPS access are ports.

The same profile-bound preparation nonce survives uncertain network outcomes.
Only an explicitly confirmed terminal record can be cleared by the store's CAS.
"""

import secrets
from contextlib import AbstractContextManager
from typing import Callable, Protocol
from agent_brain_client.domain.authorization import valid_secret
from agent_brain_client.domain.enrollment import enrollment_record
from agent_brain_client.application.installation.pairing import EnrollmentTerminal


class EnrollmentStore(Protocol):
    def lease(self) -> AbstractContextManager: ...
    def load(self) -> dict | None: ...
    def save(self, value: dict, expected: dict | None) -> None: ...
    def clear(self, expected: dict) -> None: ...


class PairingTransport(Protocol):
    def prepare(self, code: str, nonce: str, profile: dict) -> dict: ...
    def confirm(self, credential: str, nonce: str) -> dict: ...


class DeviceEnrollment:
    def __init__(
        self,
        profile: dict,
        profile_sha: str,
        store: EnrollmentStore,
        transport: PairingTransport,
        secure_input: Callable[[], str | None] | None,
    ):
        (
            self.profile,
            self.profile_sha,
            self.store,
            self.transport,
            self.secure_input,
        ) = profile, profile_sha, store, transport, secure_input

    def run(self):
        try:
            if self.transport is None:
                raise ValueError("Enrollment transport required")
            with self.store.lease():
                saved = self.store.load()
                if saved is None:
                    if self.secure_input is None:
                        raise ValueError("Secure input required")
                    code = self.secure_input()
                    if code is None:
                        return {"status": "pairing-cancelled", "installed": False}
                    if not valid_secret(code):
                        return {"status": "pairing-invalid-input", "installed": False}
                    saved = {
                        "schema": 1,
                        "profile_sha256": self.profile_sha,
                        "phase": "preparing",
                        "code": code,
                        "nonce": secrets.token_urlsafe(24),
                    }
                    # Persist before prepare. Lost replies/restarts reuse code+nonce.
                    self.store.save(saved, None)
                enrollment_record(saved, self.profile_sha, self.profile["grant"])
                if saved["phase"] == "preparing":
                    result = self.transport.prepare(
                        saved["code"], saved["nonce"], self.profile
                    )
                    if (
                        set(result)
                        != {"credential", "nonce", "device_id", "grant", "phase"}
                        or result["phase"] != "pending"
                        or result["nonce"] != saved["nonce"]
                        or result["grant"] != self.profile["grant"]
                    ):
                        raise ValueError("Unexpected prepare grant")
                    pending = {
                        "schema": 1,
                        "profile_sha256": self.profile_sha,
                        **result,
                    }
                    enrollment_record(pending, self.profile_sha, self.profile["grant"])
                    self.store.save(pending, saved)
                    saved = pending
                result = self.transport.confirm(saved["credential"], saved["nonce"])
                if result != {
                    "device_id": saved["device_id"],
                    "grant": self.profile["grant"],
                    "phase": "active",
                }:
                    raise ValueError("Unexpected confirmation")
                active = dict(saved, phase="active")
                if saved != active:
                    self.store.save(active, saved)
                return {
                    "status": "device-paired",
                    "device_id": saved["device_id"],
                    "grant": self.profile["grant"],
                    "installed": False,
                    "next_action": "Apply the separately approved exact owned content installation plan and read-only protocol check.",
                }
        except EnrollmentTerminal as error:
            return {
                "status": "pairing-reenrollment-review-required",
                "reason": error.reason,
                "installed": False,
            }
        except Exception:
            return {
                "status": "pairing-resume-required",
                "installed": False,
                "next_action": "Preserve the system credential record and retry the same pinned profile. No raw transport or credential details returned.",
            }

    def reset_terminal(self, confirm_reset):
        try:
            if self.transport is None:
                raise ValueError("Enrollment transport required")
            with self.store.lease():
                saved = self.store.load()
                if saved is None:
                    return {"status": "pairing-not-enrolled", "installed": False}
                enrollment_record(saved, self.profile_sha, self.profile["grant"])
                if saved["phase"] == "preparing":
                    try:
                        # Recheck exact code+nonce, never assume a network failure
                        # means rejection. A successful prepare stays recoverable.
                        self.transport.prepare(
                            saved["code"], saved["nonce"], self.profile
                        )
                        return {"status": "pairing-reset-blocked", "installed": False}
                    except EnrollmentTerminal as error:
                        if error.reason != "pairing_prepare_rejected":
                            raise ValueError("Unexpected prepare terminal")
                        if not confirm_reset(error.reason, "preparing"):
                            return {
                                "status": "pairing-reset-cancelled",
                                "installed": False,
                            }
                        self.store.clear(saved)
                        return {
                            "status": "pairing-reenrollment-ready",
                            "installed": False,
                        }
                try:
                    result = self.transport.confirm(saved["credential"], saved["nonce"])
                    if result != {
                        "device_id": saved["device_id"],
                        "grant": self.profile["grant"],
                        "phase": "active",
                    }:
                        raise ValueError("Unknown confirmation")
                    return {"status": "active-enrollment-preserved", "installed": False}
                except EnrollmentTerminal as error:
                    if not confirm_reset(error.reason, saved["device_id"]):
                        return {"status": "pairing-reset-cancelled", "installed": False}
                    self.store.clear(saved)
                    return {"status": "pairing-reenrollment-ready", "installed": False}
        except Exception:
            return {"status": "pairing-reset-blocked", "installed": False}
