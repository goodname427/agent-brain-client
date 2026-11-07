"""Manage approved global memory sources and native Hooks."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    root = Path(__file__).resolve().parents[4]
    sys.path[:0] = [str(root / "src"), str(root)]

import argparse
import json
import os
from agent_brain_client.runtime import collection as core


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "action",
        choices=(
            "global-install",
            "global-uninstall",
            "global-status",
            "global-hook",
            "global-configure",
            "global-recall-configure",
            "global-stop",
            "global-resume",
            "global-sync",
            "global-recall",
        ),
    )
    p.add_argument("--device-home", default=str(Path.home()))
    p.add_argument("--include-root", action="append", default=[])
    p.add_argument("--isolate-root", action="append", default=[])
    p.add_argument("--exclude-root", action="append", default=[])
    p.add_argument("--non-project-root", action="append", default=[])
    p.add_argument("--all-projects", action="store_true")
    p.add_argument("--profile-id")
    p.add_argument("--packet-output")
    p.add_argument("--activate-packet")
    p.add_argument(
        "--check-server",
        action="store_true",
        help="Authenticated metadata-only source registration check; does not enable capture",
    )
    p.add_argument(
        "--request-key",
        help="Read a specific private recall receipt with global-status",
    )
    p.add_argument(
        "--event-id",
        help="Read one event diagnostic with global-status; requires --session-id",
    )
    p.add_argument(
        "--session-id",
        help="Original capture SOURCE session ID, never a recall/reader session",
    )
    p.add_argument("--operator")
    p.add_argument("--operator-sha256")
    p.add_argument(
        "--recall-options",
        help="JSON native recall settings; maxResults is the per-namespace L1 count",
    )
    p.add_argument("--query", default="personal preferences and task progress")
    a = p.parse_args(argv)
    if a.event_id is not None or a.session_id is not None:
        if a.action != "global-status" or a.event_id is None or a.session_id is None:
            p.error(
                "--event-id and --session-id must be used together with global-status"
            )
        if a.request_key is not None:
            p.error("Event diagnostics cannot be combined with a recall --request-key")
    observation = {}
    diagnostic = {"stage": "global-settings"}
    try:
        if a.action in ("global-install", "global-uninstall"):
            from agent_brain_client.adapters.harnesses.codex.collection_installation import (
                install,
                uninstall,
            )

            result = install(a) if a.action == "global-install" else uninstall(a)
            if a.action == "global-install":
                from agent_brain_client.runtime.installation_completion import (
                    source_setup,
                )

                result["source_setup"] = source_setup(a.device_home)
        elif a.action == "global-configure":
            from agent_brain_client.runtime.source_enrollment import configure

            result = configure(a)
        else:
            # Status reads only validated metadata. An official checkout may
            # legitimately differ from the installed frozen Hook code; do not
            # use that mismatch to suppress the authenticated status probe.
            value = core.load(a.device_home, verify_code=a.action != "global-status")
            if a.action == "global-recall-configure":
                options = (
                    json.loads(a.recall_options) if a.recall_options is not None else {}
                )
                result = core.configure_recall(value, options)
            elif a.action == "global-hook":
                from agent_brain_client.application.control import RequestControl
                from agent_brain_client.adapters.storage.content import (
                    operation_diagnostics,
                )

                control = RequestControl(110)
                try:
                    with operation_diagnostics() as progress:
                        try:
                            with control.activate():
                                result = core.hook(value, observe=observation.update)
                        finally:
                            diagnostic.update(progress)
                finally:
                    control.close()
            elif a.action == "global-status":
                result = core.status(
                    value,
                    check_server=a.check_server,
                    request_key=a.request_key,
                    event_id=a.event_id,
                    session_id=a.session_id,
                )
            elif a.action == "global-resume":
                result = core.resume(value)
            elif a.action == "global-stop":
                result = core.stop(value)
            elif a.action == "global-sync":
                result = core.flush(value)
            else:
                local = core.eligible(value, os.getcwd())
                if local is None:
                    raise ValueError("Current origin not approved")
                result = {
                    "items": core.recall(
                        value, local, "global-reader-" + os.urandom(12).hex(), a.query
                    ),
                    "native_hook_verified": False,
                }
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if a.action == "global-hook" and observation:
            observation["output_written"] = True
            try:
                core.save_recall_observation(value, observation)
            except (OSError, ValueError):
                pass  # stdout is already flushed; never emit a second JSON result.
            print(
                json.dumps(
                    dict(
                        observation,
                        status="hook-output-written",
                        recall_status=observation["status"],
                    )
                ),
                file=sys.stderr,
            )
        failed_activation = (
            a.action == "global-configure" and result.get("activated") is not True
        )
        return (
            2
            if failed_activation
            or result.get("status")
            in ("global-memory-activation-required", "global-memory-resume-unconfirmed")
            else 0
        )
    except Exception as error:
        if a.event_id is not None:
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
        if a.action == "global-hook":
            print("{}")
            print(
                json.dumps(
                    {
                        "stage": "agentbrain-global-memory",
                        "status": "review-required",
                        "operation_stage": diagnostic["stage"],
                        "error_type": type(error).__name__,
                        "preserve_existing_state": True,
                    }
                ),
                file=sys.stderr,
            )
            return 0
        print(
            json.dumps(
                {
                    "status": "global-memory-review-required",
                    "operation": a.action,
                    "credentials_returned": False,
                    "preserve_existing_state": True,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
