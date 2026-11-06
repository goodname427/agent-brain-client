"""Prepare or apply one exact native installation plan."""
import sys
from pathlib import Path

if __package__ in (None, ""):
    root = Path(__file__).resolve().parents[4]
    sys.path[:0] = [str(root / "src"), str(root)]

import argparse
import hashlib
import json
from agent_brain_client.adapters.harnesses.codex import installation
from agent_brain_client.adapters.storage.pinned import pinned
from agent_brain_client.domain.contract import encode


def dispatch(args):
    if args.action == "plan":
        plan = installation.prepare(args.device_home, args.package, args.release_policy, args.release_policy_sha256, args.replace_existing)
        output = installation.safe(args.output)
        if output.exists():
            raise ValueError("Existing plan preserved")
        installation.save(output, plan)
        return {"status": "install-plan-prepared", "plan": str(output), "plan_sha256": hashlib.sha256(encode(plan)).hexdigest(), "installed": False, "native_changed": False}
    return installation.apply(pinned(args.plan_file, args.plan_sha256), args.plan_sha256)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--device-home", default=str(Path.home()))
    for name in ("package", "release-policy", "release-policy-sha256", "output", "plan-file", "plan-sha256"):
        parser.add_argument("--" + name)
    parser.add_argument("--replace-existing", action="store_true")
    args = parser.parse_args(argv)
    required = ("package", "release_policy", "release_policy_sha256", "output") if args.action == "plan" else ("plan_file", "plan_sha256")
    for name in required:
        if not getattr(args, name):
            parser.error("--" + name.replace("_", "-") + " is required")
    try:
        result = dispatch(args)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if args.action == "plan" or result.get("installed") is True else 2
    except Exception:
        print(json.dumps({"status": "client-operation-review-required", "operation": args.action, "native_state": "unconfirmed-review-required", "credentials_created": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
