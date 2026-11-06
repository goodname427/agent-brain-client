"""Start the approved MCP worker, run checks or dispatch a finite event Hook."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    root = Path(__file__).resolve().parents[4]
    sys.path[:0] = [str(root / "src"), str(root)]

import argparse
import json
from agent_brain_client.runtime import launcher as runtime
from agent_brain_client.adapters.platforms.filesystem import PlatformUnsupported


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-file", type=Path, required=True)
    parser.add_argument("--policy-sha256", required=True)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument(
        "--installation-check",
        action="store_true",
        help="Read-only content protocol/authentication check; no native mapping.",
    )
    parser.add_argument(
        "--bootstrap-check",
        action="store_true",
        help="Offline check of this launcher and the active verified package; no activation or credential access.",
    )
    parser.add_argument("--event-hook", action="store_true")
    parser.add_argument("--event-hook-policy", type=Path)
    parser.add_argument("--event-hook-policy-sha256")
    args = parser.parse_args(argv)
    try:
        if bool(args.event_hook_policy) != bool(
            args.event_hook_policy_sha256
        ) or args.event_hook != bool(args.event_hook_policy):
            raise ValueError("Exact finite hook policy required")
        result = runtime.startup(
            args.policy_file,
            args.policy_sha256,
            args.check_only,
            args.installation_check,
            args.bootstrap_check,
            (args.event_hook_policy, args.event_hook_policy_sha256)
            if args.event_hook
            else None,
        )
    except PlatformUnsupported as error:
        result = error.result(served=False, native_changed=False)
    except Exception:
        result = {
            "status": "startup-review-required",
            "served": False,
            "next_action": "Inspect the approved fixed bootstrap, policy digest and isolated client paths; preserve existing packages.",
        }
    if args.check_only or args.bootstrap_check:
        print(json.dumps(result))
    elif not result.get("served"):
        print(json.dumps(result), file=sys.stderr)
    if args.event_hook:
        # A capture failure must not block UserPromptSubmit or force another
        # model continuation. Keep failure/pending evidence, return no context.
        if not result.get("served"):
            print("{}")
        elif result.get("exit_code", 0) != 0:
            # The host sees success by design. Preserve a bounded indication
            # that dispatch reached this launcher but its worker failed, even
            # when that worker could not emit its own startup diagnostic.
            print(
                json.dumps(
                    {"stage": "event-hook-worker", "error_code": "hook_worker_failed"}
                ),
                file=sys.stderr,
            )
        return 0
    checked = (
        args.check_only
        and result["status"] in ("current", "updated", "offline")
        and bool(result.get("active"))
    )
    bootstrap_checked = (
        args.bootstrap_check and result["status"] == "client-bootstrap-verified"
    )
    return result.get("exit_code", 0 if checked or bootstrap_checked else 2)


if __name__ == "__main__":
    sys.exit(main())
