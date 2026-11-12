"""interfaces / cli / install: concrete installation responsibility."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    _ENTRY_ROOT = Path(__file__).resolve().parents[4]
    sys.path[:0] = [str(_ENTRY_ROOT / "src"), str(_ENTRY_ROOT)]


import argparse
import json
from agent_brain_client.adapters.platforms.filesystem import require_platform
from agent_brain_client.domain.service import endpoint_check

ROOT = Path(__file__).resolve().parents[4]
SOURCE = ROOT
OFFICIAL_ORIGIN = "https://github.com/goodname427/agent-brain-client.git"
OFFICIAL_REF = "refs/heads/releases/personal-https-stable"


def lifecycle_exit(action, value):
    """Exit success denotes the requested action, never merely an old installation."""
    status = value.get("status")
    if action == "install" and value.get("installation_stages", {}).get("setup", {}).get("stage") == "native-mcp":
        return 2
    if (
        value.get("memory_accessibility", {}).get("status")
        == "review-or-resume-required"
    ):
        return 2
    if (
        action == "check-update"
        and status in ("client-update-available", "client-update-current")
        and value.get("channel_checked") is True
    ):
        return 0
    if (
        action == "update"
        and status in ("client-update-updated", "client-update-current")
        and value.get("channel_checked") is True
    ):
        return 0
    if action in ("status", "repair", "uninstall") and status in (
        "client-uninstalled",
        "client-not-installed",
    ):
        return 0
    if (
        action in ("status", "repair")
        and status == "content-native-client-disabled"
        and value.get("registration_verified") is True
    ):
        return 0
    if (
        action in ("status", "repair", "install")
        and status == "content-native-client-installed"
        and value.get("installed") is True
    ):
        return 0
    return 2


def selected_native(home, harness):
    if harness not in ("codex", "codebuddy"):
        raise ValueError("Install and reinstall require an explicit --harness")
    from agent_brain_client.adapters.storage.registration import registry_home
    native = registry_home(home)
    if (harness == "codex" and native == Path(home) / ".config/agent-brain-client/shared-native"
            and not (Path(home) / ".config/agent-brain-client/codex-mcp.json").exists()):
        from agent_brain_client.adapters.harnesses.codex.binding import preflight
        preflight(home)
    return native


def main():
    from agent_brain_client.runtime.installation import ConnectionRequired
    from agent_brain_client.runtime.installation import confirm_target
    from agent_brain_client.runtime.installation import finish_install
    from agent_brain_client.runtime.updates import manage_update
    from agent_brain_client.runtime.installation import recover_windows_setup
    from agent_brain_client.runtime.updates import reexec_update_runtime
    from agent_brain_client.runtime.installation import resolve_connection
    from agent_brain_client.runtime.installation import run

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", choices=("codex", "codebuddy"))
    parser.add_argument(
        "--action",
        choices=(
            "install",
            "reinstall",
            "status",
            "repair",
            "uninstall",
            "check-update",
            "update",
        ),
        default="install",
    )
    parser.add_argument(
        "--server",
        help="Your selected credential-free HTTPS Server origin; access key is requested only in the local hidden input.",
    )
    parser.add_argument("--connection", type=Path)
    parser.add_argument("--expected-connection-sha256")
    args = parser.parse_args()
    fresh_requested = args.action == "reinstall"
    fresh_stage = "retention"

    def finish_result(value):
        from agent_brain_client.adapters.storage.reinstallation import read_journal

        fresh = read_journal(Path.home().resolve())
        if fresh is not None:
            value = dict(
                value,
                retained_archive=fresh["archive"],
                old_identity_reused=False,
                old_queue_replayed=False,
            )
        if (
            value.get("installed") is True
            or fresh is not None
            and value.get("registration_verified") is True
        ):
            from agent_brain_client.runtime.fresh_installation import verify_memory

            try:
                accessibility = verify_memory(Path.home().resolve())
                if accessibility is not None:
                    value = dict(value, memory_accessibility=accessibility)
            except Exception:
                return dict(
                    value,
                    installation_complete=False,
                    memory_accessibility={"status": "review-or-resume-required"},
                    next_action="Rerun reinstall with the same Server/account; preserve the retained archive. Mapping and Hook setup wait for readable retained memories. Old queues are never replayed.",
                )
        return finish_install(Path.home().resolve(), value, harness=args.harness)

    try:
        require_platform("first-install")
        home = Path.home().resolve()
        if args.action in ("check-update", "update"):
            if (
                args.server is not None
                or args.connection is not None
                or args.expected_connection_sha256 is not None
            ):
                value = {
                    "status": "client-update-review-required",
                    "reason": "existing-policy-only",
                    "native_changed": False,
                    "device_credentials_accessed": False,
                    "next_action": "Update management reuses the verified registration; do not pass a replacement connection or invitation.",
                }
            else:
                try:
                    delegated = reexec_update_runtime(home, args.action)
                    if delegated is not None:
                        return delegated
                    value = manage_update(home, args.action)
                except Exception:
                    value = {
                        "operation": args.action,
                        "status": "client-update-review-required",
                        "reason": "existing-runtime-or-registration-unverified",
                        "channel_checked": False,
                        "native_changed": False,
                        "device_credentials_accessed": False,
                        "next_action": "Restore the verified original runtime/registration; do not prepare new dependencies, replace bindings or re-pair.",
                    }
            print(json.dumps(value))
            return lifecycle_exit(args.action, value)
        if fresh_requested:
            if args.connection or args.expected_connection_sha256:
                raise ValueError(
                    "Fresh reconnection uses the Server address and hidden access key only"
                )
            from agent_brain_client.runtime.fresh_installation import (
                retained_installation,
            )
            from agent_brain_client.application.installation.fresh import (
                prepare_fresh_installation,
            )
            from agent_brain_client.adapters.storage.reinstallation import read_journal

            selected_native(home, args.harness)  # Validate the explicit host.
            from agent_brain_client.adapters.storage.registration import registry_home
            native = registry_home(home, retained=True)
            port = retained_installation(home, native)
            with port.lease():
                prepared = prepare_fresh_installation(
                    port, lambda message: input(message + "\n"), server=args.server
                )
            if prepared["status"] != "fresh-install-ready":
                print(json.dumps(prepared))
                return 2
            saved = read_journal(home)
            if args.server is None:
                args.server = saved["endpoint"]
            args.action = "install"
            fresh_stage = "reconnection"
        if args.action != "install":
            from agent_brain_client.runtime.client_lifecycle import (
                status,
                repair,
                uninstall,
            )

            try:
                value = (
                    uninstall(home, confirm_target)
                    if args.action == "uninstall"
                    else repair(home)
                    if args.action == "repair"
                    else status(home)
                )
            except RuntimeError:
                value = {
                    "status": "client-in-use",
                    "native_changed": False,
                    "next_action": "Disable only agent_brain_server in the host Settings > MCP servers, save and use the host Restart/reload. Wait for this MCP worker to exit before rerunning. If that service cannot be paused or the worker remains active, stop and report the blocker. Preserve other MCP settings and persistent locks.",
                }
            except Exception:
                partial = False
                if args.action in ("uninstall", "repair"):
                    from agent_brain_client.runtime.client_lifecycle import (
                        journal_path,
                        read_removal,
                    )

                    try:
                        partial = (
                            journal_path(home).exists()
                            and read_removal(home).get("phase") == "applying"
                        )
                    except Exception:
                        partial = True
                value = {
                    "status": "uninstall-recovery-review-required"
                    if partial
                    else "client-ownership-review-required",
                    "native_changed": None if partial else False,
                    "next_action": "Preserve local files and recovery receipts; do not delete data or reset Keychain. Use status, then repair for interrupted uninstall.",
                }
            print(json.dumps(value))
            return lifecycle_exit(args.action, value)
        native = selected_native(home, args.harness)
        access_state = (
            home / ".config/agent-brain-client/access-bootstrap/endpoint.json"
        )
        registration = home / ".config/agent-brain-client/codex-mcp.json"
        removal = home / ".config/agent-brain-client/uninstall-receipt.json"
        if args.server and (args.connection or args.expected_connection_sha256):
            raise ValueError(
                "Choose the address/key entry or the legacy connection entry"
            )
        recovery = recover_windows_setup(home, native)
        if recovery is not None:
            print(json.dumps(recovery))
            return 2
        if (
            registration.exists()
            and not removal.exists()
            and access_state.exists()
            and not args.connection
            and not args.expected_connection_sha256
        ):
            from agent_brain_client.interfaces.cli.access import (
                resume_applying_registration,
            )

            resumed = resume_applying_registration(home, native, args.server, harness=args.harness)
            if resumed is not None:
                from agent_brain_client.application.installation.follow_up import (
                    installation_follow_up,
                )

                resumed = finish_result(installation_follow_up(resumed))
                print(json.dumps(resumed))
                return lifecycle_exit("install", resumed)
        if (
            not registration.exists()
            and not removal.exists()
            and not args.connection
            and not args.expected_connection_sha256
            and (args.server or access_state.parent.exists())
        ):
            from agent_brain_client.interfaces.cli.access import run_address_install

            value = run_address_install(home, native, args.server, harness=args.harness)
            from agent_brain_client.application.installation.follow_up import (
                installation_follow_up,
            )

            value = finish_result(installation_follow_up(value))
            print(json.dumps(value))
            return lifecycle_exit("install", value)
        try:
            connection, connection_sha = resolve_connection(
                home, native, args.connection, args.expected_connection_sha256
            )
        except ConnectionRequired:
            from agent_brain_client.interfaces.cli.access import run_address_install

            value = run_address_install(home, native, args.server, harness=args.harness)
            from agent_brain_client.application.installation.follow_up import (
                installation_follow_up,
            )

            value = finish_result(installation_follow_up(value))
            print(json.dumps(value))
            return lifecycle_exit("install", value)
        except Exception:
            print(
                json.dumps(
                    {
                        "status": "connection-file-review-required"
                        if args.connection
                        else "connection-recovery-review-required",
                        "installed": False,
                        "native_changed": False,
                        "credential_accessed": False,
                        "next_action": "Preserve connection metadata, original profile/policy and recovery receipts. Review the original binding; do not reset identity, replace digests or use an example connection.",
                    }
                )
            )
            return 2
        if args.server is not None and endpoint_check(args.server) != endpoint_check(
            connection["endpoint"]
        ):
            raise ValueError("Existing Server identity preserved")
        value = run(home, native, connection, connection_sha, harness=args.harness)
        from agent_brain_client.application.installation.follow_up import (
            installation_follow_up,
        )

        value = finish_result(installation_follow_up(value))
        print(json.dumps(value))
        return lifecycle_exit("install", value)
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "fresh-install-review-or-resume-required"
                    if fresh_requested
                    else "first-install-review-or-resume-required",
                    "installed": False,
                    "native_changed": None if fresh_requested else False,
                    **(
                        {
                            "next_action": "Close all old host workers, preserve the retained files and rerun the SAME reinstall command. Review any configuration drift; do not delete state or edit pins.",
                            "stage": fresh_stage,
                            "reason": type(error).__name__,
                        }
                        if fresh_requested
                        else {}
                    ),
                }
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
