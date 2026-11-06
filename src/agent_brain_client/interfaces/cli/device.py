"""Explicit device enrollment and task commands over the approved transport."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    _ENTRY_ROOT = Path(__file__).resolve().parents[4]
    sys.path[:0] = [str(_ENTRY_ROOT / "src"), str(_ENTRY_ROOT)]


import argparse
import hashlib
import json

ROOT = Path(__file__).resolve().parents[4]
from agent_brain_client.adapters.server.https import (
    profile_file,
    PinnedDeviceTask,
    HTTPSContentClient,
    native_store,
    DeviceEnrollment,
    PairingHTTPS,
)
from agent_brain_client.adapters.platforms.credentials import owned, unique
from agent_brain_client.adapters.platforms.filesystem import PlatformUnsupported
from agent_brain_client.runtime.enrollment import install_once


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "inspect",
            "pair",
            "reset-terminal",
            "install",
            "task-check",
            "task-execute",
        ),
    )
    parser.add_argument("--https-profile")
    parser.add_argument("--https-profile-sha256")
    parser.add_argument("--device-task-policy")
    parser.add_argument("--device-task-policy-sha256")
    parser.add_argument("--task-request-file")
    parser.add_argument("--task-request-sha256")
    parser.add_argument("--content-install-plan")
    parser.add_argument("--approved-plan-sha256")
    args = parser.parse_args()
    if args.action == "install":
        try:
            from agent_brain_client.adapters.storage.pinned import pinned

            result = install_once(
                pinned(args.content_install_plan, args.approved_plan_sha256),
                args.approved_plan_sha256,
            )
        except PlatformUnsupported as error:
            result = error.result(installed=False, native_changed=False)
        except Exception:
            result = {
                "status": "https-install-review-required",
                "installed": False,
                "native_changed": False,
            }
        print(json.dumps(result))
        return 0 if result.get("installed") else 2
    try:
        profile = profile_file(args.https_profile, args.https_profile_sha256)
        if args.action in ("task-check", "task-execute"):
            task = PinnedDeviceTask(
                args.device_task_policy,
                args.device_task_policy_sha256,
                profile,
                args.https_profile_sha256,
            )
            raw = owned(args.task_request_file).read_bytes()
            if (
                len(raw) > 4096
                or hashlib.sha256(raw).hexdigest() != args.task_request_sha256
            ):
                raise ValueError("Pinned request required")
            request = json.loads(raw, object_pairs_hook=unique)
            task.check(request, task.value["device_id"])
            if args.action == "task-check":
                result = {
                    "status": "device-task-metadata-checked",
                    "credential_accessed": False,
                    "http_requests": 0,
                    "authorization_verified": False,
                }
            else:
                client = HTTPSContentClient(
                    args.https_profile,
                    args.https_profile_sha256,
                    device_task=(
                        args.device_task_policy,
                        args.device_task_policy_sha256,
                    ),
                )
                result = client.request(
                    request
                )  # Exactly one call; never enqueue or retry.
        elif args.action == "inspect":
            result = {
                "status": "https-content-profile-reviewed",
                "credential_accessed": False,
                "installed": False,
                "native_changed": False,
                "methods": ["content_catalog", "content_read", "content_put"],
            }
        else:
            store = native_store(profile, args.https_profile_sha256)
            enrollment = DeviceEnrollment(
                profile,
                args.https_profile_sha256,
                store,
                PairingHTTPS(profile["endpoint"]),
                store.backend.secure_input,
            )
            result = (
                enrollment.run()
                if args.action == "pair"
                else enrollment.reset_terminal(store.backend.confirm_reset)
            )
    except PlatformUnsupported as error:
        result = error.result(installed=False, native_changed=False)
    except Exception:
        result = {
            "status": "https-device-review-required",
            "installed": False,
            "native_changed": False,
        }
    print(json.dumps(result))
    return (
        0
        if "error" not in result
        and (
            args.action == "task-execute"
            and "result" in result
            or result.get("status")
            in (
                "device-paired",
                "https-content-profile-reviewed",
                "device-task-metadata-checked",
            )
        )
        else 2
    )


if __name__ == "__main__":
    sys.exit(main())
