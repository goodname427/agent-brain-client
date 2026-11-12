"""Project memory commands over the enrolled Client and an explicit native host."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    root = Path(__file__).resolve().parents[4]
    sys.path[:0] = [str(root / "src"), str(root)]

import argparse
import json
import os
from agent_brain_client.adapters.storage.metadata import canonical, digest, private_json
from agent_brain_client.adapters.platforms.filesystem import file_mode
from agent_brain_client.adapters.storage.project_settings import load, active, project_settings, project_harness
from agent_brain_client.runtime.project_memory import install, context
from agent_brain_client.runtime.project_memory import (
    setup,
    enable,
    import_bundle,
    activate_bundle,
    daily,
    daily_command,
    project_link,
    connect_project,
    hook,
    record,
    recalled,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "setup",
            "enable",
            "import-bundle",
            "activate-bundle",
            "server-status",
            "project-link",
            "connect-project",
            "revoke",
            "resume",
            "flush",
            "sync",
            "install",
            "status",
            "recall",
            "record",
            "hook",
            "stop",
        ),
    )
    parser.add_argument("--harness", choices=("codex", "codebuddy"))
    parser.add_argument("--device-home", default=str(Path.home()))
    parser.add_argument("--project-root")
    parser.add_argument("--output")
    parser.add_argument("--query", default="")
    parser.add_argument("--activation")
    parser.add_argument("--activation-sha256")
    parser.add_argument("--server-bundle")
    parser.add_argument("--server-bundle-sha256")
    parser.add_argument("--settings")
    parser.add_argument("--settings-sha256")
    parser.add_argument(
        "--session-id",
        help="For event diagnostics, the original capture SOURCE session ID",
    )
    parser.add_argument(
        "--event-id",
        help="Read one event diagnostic with server-status; requires --session-id",
    )
    parser.add_argument("--input")
    args = parser.parse_args()
    if args.event_id is not None and (
        args.action != "server-status" or args.session_id is None
    ):
        parser.error("--event-id requires server-status and the original --session-id")
    if (
        args.action == "server-status"
        and args.session_id is not None
        and args.event_id is None
    ):
        parser.error("server-status --session-id requires --event-id")
    try:
        if args.harness is None:
            if args.settings and args.settings_sha256:
                args.harness = project_harness(private_json(args.settings, args.settings_sha256))
            else:
                from agent_brain_client.runtime.client_lifecycle import registered_host
                port = registered_host(args.device_home)
                if port is None:
                    raise ValueError("Registered host or explicit --harness required")
                args.harness = port.name
        if args.action == "connect-project":
            result = connect_project(args)
        elif args.action == "activate-bundle":
            result = activate_bundle(args)
        elif args.action == "enable":
            result = enable(args)
        elif args.action == "import-bundle":
            if not args.activation or not args.activation_sha256:
                parser.error(
                    "import-bundle requires --activation and --activation-sha256"
                )
            result = import_bundle(args)
        elif args.action == "setup":
            if not args.project_root or not args.output:
                parser.error("setup requires --project-root and --output")
            result = setup(args)
        else:
            if not args.settings and args.project_root:
                args.settings = str(
                    project_settings(args.device_home, args.project_root, harness=args.harness)
                )
                private_json(args.settings)
                args.settings_sha256 = digest(Path(args.settings).read_bytes())
            if not args.settings or not args.settings_sha256:
                parser.error("exact --settings and --settings-sha256 are required")
            p = load(args.settings, args.settings_sha256)
            if p["harness"] != args.harness:
                raise ValueError("Project Harness binding changed")
            if args.action == "install":
                result = install(p, args.settings, args.settings_sha256)
            elif args.action in ("stop", "revoke"):
                marker = canonical(p["off_file"])
                with marker.open("ab") as stream:
                    file_mode(stream.fileno(), 0o600)
                    stream.flush()
                    os.fsync(stream.fileno())
                remote = None
                if daily(p) and p["capture_policy"] is not None:
                    try:
                        remote = daily_command(p, args.action)
                    except Exception:
                        remote = {
                            "server_stop_confirmed": False,
                            "local_source_stopped": True,
                        }
                result = {
                    "status": "project-memory-"
                    + ("revoked" if args.action == "revoke" else "stopped"),
                    "local_source_stopped": True,
                    "server": remote,
                }
            elif args.action == "status":
                result = {
                    "status": "enabled" if active(p) else "stopped",
                    "source_mode": p["source_mode"],
                    "capture_configured": p["capture_policy"] is not None,
                    "native_delivery": "unobserved",
                    "model_configuration_owned_by_server": True,
                    "memory_read": False,
                    "credential_accessed": False,
                    "project_id": private_json(
                        p["recall_policy"], p["recall_policy_sha256"]
                    )["project_id"]
                    if p["recall_policy"]
                    else None,
                    "notes_directory": private_json(
                        p["capture_policy"], p["capture_policy_sha256"]
                    ).get("notes_directory")
                    if p["capture_policy"]
                    else None,
                    "lifecycle": "until-disabled"
                    if daily(p)
                    else "legacy-or-shared-memory",
                }
            elif args.action == "project-link":
                if not args.output:
                    raise ValueError("Explicit private --output required")
                result = project_link(p, args.output)
            elif args.action in ("server-status", "resume", "flush", "sync"):
                result = daily_command(
                    p,
                    args.action,
                    event_id=args.event_id,
                    session_id=args.session_id if args.event_id is not None else None,
                )
            elif args.action == "hook":
                result = hook(p, settings_path=args.settings, settings_sha256=args.settings_sha256)
            elif args.action == "record":
                if not args.input:
                    parser.error("record requires an explicit private --input file")
                result = record(p, args.input)
            else:
                if not active(p) and not daily(p):
                    raise ValueError("Project memory is stopped")
                result = {
                    "additionalContext": context(p, recalled(p, args.session_id)),
                    "model_calls_sent": 0,
                }
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        if args.event_id is not None:
            from agent_brain_client.domain.errors import EventDiagnosticsUnavailable

            print(
                json.dumps(
                    {
                        "status": "event-diagnostics-unavailable",
                        "reason": "server-diagnostics-unsupported"
                        if isinstance(error, EventDiagnosticsUnavailable)
                        else "diagnostics-unconfirmed",
                        "credentials_returned": False,
                        "preserve_existing_state": True,
                    }
                )
            )
            return 2
        if args.action == "hook":
            print("{}")
            print(
                '{"stage":"agentbrain-memory","status":"review-required"}',
                file=sys.stderr,
            )
            return 0
        print(
            json.dumps(
                {
                    "status": "memory-operation-review-required",
                    "operation": args.action,
                    "credentials_returned": False,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
