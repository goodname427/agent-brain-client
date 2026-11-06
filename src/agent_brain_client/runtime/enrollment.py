"""Verified native enrollment and installation transaction."""

import hashlib
from pathlib import Path
from agent_brain_client.adapters.platforms.filesystem import PlatformUnsupported
from agent_brain_client.adapters.server.https import (
    profile_file,
    native_store,
    DeviceEnrollment,
    PairingHTTPS,
)


def install_once(
    plan,
    approved_digest,
    *,
    store=None,
    transport=None,
    secure_input=None,
    installer=None,
):
    """One approved native plan: verify before UI, resume pairing, then install.

    This does not create an invitation, read a token from argv or broaden an
    existing profile. Pairing recovery remains in the same dedicated OS item.
    """
    from agent_brain_client.adapters.harnesses.codex.installation import (
        context,
        prepare,
        apply,
    )
    from agent_brain_client.adapters.storage.private_files import private_json
    from agent_brain_client.domain.errors import InstallBlocked
    from agent_brain_client.domain.contract import encode as plan_bytes

    try:
        if (
            not isinstance(plan, dict)
            or hashlib.sha256(plan_bytes(plan)).hexdigest() != approved_digest
        ):
            raise InstallBlocked("Exact approved native plan required")
        policy, _, _, _ = context(
            plan["device_home"],
            plan["package"],
            plan["release_policy"],
            plan["release_policy_sha256"],
        )
        from agent_brain_client.adapters.packages.client import (
            HTTPS_CONTENT_CAPABILITIES,
        )

        if policy["capabilities"] != HTTPS_CONTENT_CAPABILITIES:
            raise InstallBlocked("HTTPS install scope required")
        runtime = policy["runtime"]
        profile = profile_file(
            runtime["https_profile"], runtime["https_profile_sha256"]
        )
        install = installer or apply
        receipt = (
            Path(plan["device_home"])
            / ".config/agent-brain-client/content-install-receipt.json"
        )
        if receipt.exists():
            previous = private_json(receipt)
            removed = (
                Path(plan["device_home"])
                / ".config/agent-brain-client/uninstall-receipt.json"
            )
            reinstallation = (
                removed.exists() and not (receipt.parent / "codex-mcp.json").exists()
            )
            if (
                previous.get("phase") in ("complete", "applying")
                and previous.get("plan_sha256") == approved_digest
                and (previous.get("phase") == "applying" or not reinstallation)
            ):
                # Existing apply verifies/restores an interrupted matching journal
                # before checking the original preimage; pairing already preceded it.
                return install(plan, approved_digest)
        if (
            prepare(
                plan["device_home"],
                plan["package"],
                plan["release_policy"],
                plan["release_policy_sha256"],
                plan["replace_existing"],
            )
            != plan
        ):
            raise InstallBlocked("Install preimage changed before credential input")
        credentials = (
            store
            if store is not None
            else native_store(profile, runtime["https_profile_sha256"])
        )
        enrollment = DeviceEnrollment(
            profile,
            runtime["https_profile_sha256"],
            credentials,
            transport if transport is not None else PairingHTTPS(profile["endpoint"]),
            secure_input
            if secure_input is not None
            else credentials.backend.secure_input,
        )
        paired = enrollment.run()
        if paired.get("status") != "device-paired":
            return {
                "status": paired.get("status", "pairing-resume-required"),
                "installed": False,
                "native_changed": False,
                "credential_record_preserved": True,
                **({"reason": paired["reason"]} if "reason" in paired else {}),
            }
        return install(plan, approved_digest)
    except PlatformUnsupported as error:
        return error.result(
            installed=False, native_changed=False, credential_record_preserved=True
        )
    except Exception:
        return {
            "status": "https-install-review-or-resume-required",
            "installed": False,
            "native_changed": False,
            "credential_record_preserved": True,
        }
