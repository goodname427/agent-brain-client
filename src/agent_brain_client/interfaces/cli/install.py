"""interfaces / cli / install: concrete installation responsibility."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    _ENTRY_ROOT = Path(__file__).resolve().parents[4]
    sys.path[:0] = [str(_ENTRY_ROOT / "src"), str(_ENTRY_ROOT)]


import argparse
import json
import stat

ROOT = Path(__file__).resolve().parents[4]
SOURCE = ROOT
OFFICIAL_ORIGIN = "https://github.com/goodname427/agent-brain-client.git"
OFFICIAL_REF = "refs/heads/releases/personal-https-stable"


def lifecycle_exit(action, value):
    """Exit success denotes the requested action, never merely an old installation."""
    if action == "reinstall":
        action = "install"
    status = value.get("status")
    if action == "status" and value.get("requested_harness_status", {}).get("status") in (
        "review-required", "codebuddy-review-or-resume-required",
    ):
        return 2
    if action in ("install", "update"):
        stages = value.get("installation_stages", {})
        setup = stages.get("setup", {})
        sources = stages.get("sources") or value.get("source_setup", {})
        source_status = sources.get("status")
        source_preserved = (
            source_status == "global-memory-source-preserved"
            and (action == "update" or value.get("update_check_performed") is False)
        )
        launcher = value.get("launcher_update", {})
        if (
            setup.get("stage") is not None
            or stages.get("mapping", {}).get("status") == "mapping-pending"
            or (
                sources.get("collection_enabled") is False
                and source_status not in (
                    "paused", "retained-source-paused", "global-memory-source-paused",
                )
                and not source_preserved
            )
            or (
                launcher
                and launcher.get("status") not in ("client-update-current", "client-update-updated")
            )
        ):
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
        action in ("status", "repair", "install")
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
    if action == "unbind" and status in (
        "host-unbound", "codebuddy-uninstalled", "codebuddy-not-installed",
    ):
        return 0
    if action == "probe" and value.get("protocol_checked") is True:
        return 0
    if action == "reinstall-check" and status == "fresh-install-runtime-idle":
        return 0
    return 2


def report(action, value):
    code = lifecycle_exit(action, value)
    outcome = "completed" if code == 0 else "review-required"
    if code and action in ("install", "reinstall", "update") and (
        value.get("installation_stages") or value.get("channel_checked") is True
    ):
        outcome = "partial"
    print(json.dumps(dict(value, operation=action, outcome=outcome)))
    return code


def absent_status(home):
    """Only the absence of all Client state roots proves a cold local status."""
    config = home / ".config"
    try:
        try:
            if not stat.S_ISDIR(config.lstat().st_mode):
                return False
        except FileNotFoundError:
            return True
        for name in ("agent-brain-client", "agent-brain-reinstallation"):
            try:
                (config / name).lstat()
            except FileNotFoundError:
                continue
            return False
    except OSError:
        return False
    return True


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


class InstallerArguments(argparse.ArgumentParser):
    def error(self, message):
        self.exit(report("arguments", {
            "status": "invalid-install-arguments", "reason": "invalid-options",
            "next_action": "Use the documented installer actions and options. See --help.",
        }))


def parse_arguments(argv=None):
    parser = InstallerArguments(prog="Agent Brain Client installer",
                                description="Agent Brain Client installation and maintenance")
    parser.add_argument("--harness", choices=("codex", "codebuddy"))
    parser.add_argument(
        "--action",
        choices=(
            "install",
            "reinstall",
            "reinstall-check",
            "status",
            "probe",
            "repair",
            "unbind",
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
    parser.add_argument("--access-key-file", type=Path, help="Read a user-selected local key file without interactive input; never pass the key itself.")
    parser.add_argument("--connection", type=Path)
    parser.add_argument("--expected-connection-sha256")
    parser.add_argument("--surface", choices=("ide", "cli", "both"))
    args = parser.parse_args(argv)
    if args.action in ("install", "reinstall"):
        args.harness = args.harness or "codex"
    if args.surface is not None and (
        args.harness != "codebuddy" or args.action != "install"
    ):
        parser.error("An explicit CodeBuddy surface applies to install")
    if args.action == "unbind" and args.harness is None:
        parser.error("Use an explicit Harness for unbind")
    if args.action not in ("install", "reinstall") and (
        args.server is not None or args.access_key_file is not None
        or args.connection is not None or args.expected_connection_sha256 is not None
    ):
        if args.action in ("check-update", "update"):
            raise SystemExit(report(args.action, {
                "status": "client-update-review-required",
                "reason": "existing-policy-only",
                "native_changed": False,
                "device_credentials_accessed": False,
                "next_action": "Update management reuses the verified registration. Rerun without connection inputs.",
            }))
        parser.error("Only install or reinstall accepts connection inputs")
    if args.connection is not None or args.expected_connection_sha256 is not None:
        if args.action == "reinstall" or args.server is not None or args.access_key_file is not None:
            parser.error("Choose the address and key entry or the legacy connection entry")
    return args


def main(argv=None):
    try:
        args = parse_arguments(argv)
    except SystemExit as error:
        return error.code
    from agent_brain_client.adapters.platforms.filesystem import require_platform
    from agent_brain_client.domain.service import endpoint_check
    from agent_brain_client.runtime.installation import (
        ConnectionRequired, confirm_target, finish_install, recover_windows_setup,
        resolve_connection, run,
    )
    from agent_brain_client.runtime.updates import manage_update, reexec_update_runtime

    operation = args.action
    fresh_requested = args.action == "reinstall"
    from agent_brain_client.adapters.storage.reinstallation import RuntimeInUse
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
        home = Path.home().resolve()
        if args.harness != "codebuddy":
            return finish_install(home, value, harness=args.harness)
        from agent_brain_client.adapters.storage.collection import settings_path
        from agent_brain_client.interfaces.cli.codebuddy import (
            binding_review, configure_binding,
        )

        # An existing registration may already have a prepared source. Repeating
        # install or adding CodeBuddy must not run the updater or change that scope.
        existing = value.get("update_check_performed") is False
        if not (existing and settings_path(home).exists()):
            value = finish_install(home, value, harness=args.harness)
        if (
            value.get("installed") is not True
            or value.get("installation_stages", {}).get("setup")
            or value.get("launcher_update", {}).get("status") not in (
                None, "client-update-current", "client-update-updated"
            )
        ):
            return value
        stages = value.setdefault("installation_stages", {})
        try:
            binding = configure_binding(home, surface=args.surface)
        except (OSError, RuntimeError, ValueError) as error:
            stages["native_mcp"] = binding_review(operation, error)
            stages["setup"] = {"status": "pending-review-or-resume", "stage": "native-mcp"}
            value["next_action"] = stages["native_mcp"]["next_action"]
        else:
            stages["native_mcp"] = binding
            value["native_changed"] = bool(value.get("native_changed") or binding["native_changed"])
        return value

    try:
        key_options = {}
        if args.access_key_file is not None:
            from agent_brain_client.interfaces.cli.access import file_key_input
            key_options["key_input"] = file_key_input(args.access_key_file)
        require_platform("first-install")
        home = Path.home().resolve()
        if args.action in ("probe", "unbind"):
            from agent_brain_client.interfaces.cli.codebuddy import binding_review, probe
            from agent_brain_client.runtime.client_lifecycle import unbind

            try:
                value = (
                    unbind(home, harness=args.harness)
                    if args.action == "unbind"
                    else probe(home, None, harness=args.harness or "codex")
                )
            except (OSError, RuntimeError, ValueError) as error:
                value = binding_review(operation, error)
                if args.action == "probe" and args.harness != "codebuddy":
                    value["status"] = "client-probe-review-required"
                    value["next_action"] = "Verify the existing Client registration and retry the read-only protocol probe."
                if args.action == "unbind":
                    value["status"] = "host-unbind-review-required"
                    value["harness"] = args.harness
                    value["next_action"] = "Verify the selected native binding and its recovery receipt, then retry unbind."
            return report(operation, value)
        if args.action == "reinstall-check":
            from agent_brain_client.runtime.fresh_installation import retained_installation
            from agent_brain_client.adapters.storage.registration import registry_home

            value = retained_installation(home, registry_home(home, retained=True)).runtime_status()
            return report(operation, value)
        if args.action in ("check-update", "update"):
            try:
                delegated = reexec_update_runtime(home, args.action)
                if delegated is not None:
                    return delegated
                value = manage_update(home, args.action)
            except Exception:
                value = {
                    "status": "client-update-review-required",
                    "reason": "existing-runtime-or-registration-unverified",
                    "channel_checked": False,
                    "native_changed": False,
                    "device_credentials_accessed": False,
                    "next_action": "Restore the verified original runtime/registration; do not prepare new dependencies, replace bindings or re-pair.",
                }
            if args.action == "update":
                from agent_brain_client.runtime.installation_completion import finish_windows_update
                value = finish_windows_update(home, value)
            return report(operation, value)
        if fresh_requested:
            from agent_brain_client.runtime.fresh_installation import (
                retained_installation,
            )
            from agent_brain_client.application.installation.fresh import (
                prepare_fresh_installation,
            )
            from agent_brain_client.adapters.storage.reinstallation import read_journal

            pending = read_journal(home)
            if pending is not None and pending["phase"] != "complete":
                if args.harness not in ("codex", "codebuddy"):
                    raise ValueError("Reinstall recovery requires an explicit --harness")
                # Recovery is bound to its journal and verified detached images,
                # not a new host enrollment against partly archived metadata.
            else:
                selected_native(home, args.harness)
            from agent_brain_client.adapters.storage.registration import registry_home
            native = registry_home(home, retained=True)
            port = retained_installation(home, native)

            def choose(message):
                print(message, file=sys.stderr)
                return input()

            with port.lease():
                prepared = prepare_fresh_installation(
                    port, choose, server=args.server
                )
            if prepared["status"] != "fresh-install-ready":
                return report(operation, prepared)
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
            if args.action == "status" and args.harness:
                value["requested_harness"] = args.harness
                requested = value.get("host_installations", {}).get(args.harness)
                if requested is None and lifecycle_exit("status", value) == 0:
                    requested = {"status": "host-not-installed", "installed": False}
                value["requested_harness_status"] = requested or {"status": "review-required"}
            return report(operation, value)
        native = selected_native(home, args.harness)
        access_state = (
            home / ".config/agent-brain-client/access-bootstrap/endpoint.json"
        )
        registration = home / ".config/agent-brain-client/codex-mcp.json"
        removal = home / ".config/agent-brain-client/uninstall-receipt.json"
        recovery = recover_windows_setup(home, native)
        if recovery is not None:
            return report(operation, recovery)
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
                return report(operation, resumed)
        if (
            not registration.exists()
            and not removal.exists()
            and not args.connection
            and not args.expected_connection_sha256
            and (args.server or access_state.parent.exists())
        ):
            from agent_brain_client.interfaces.cli.access import run_address_install

            value = run_address_install(home, native, args.server, harness=args.harness, **key_options)
            from agent_brain_client.application.installation.follow_up import (
                installation_follow_up,
            )

            value = finish_result(installation_follow_up(value))
            return report(operation, value)
        try:
            connection, connection_sha = resolve_connection(
                home, native, args.connection, args.expected_connection_sha256
            )
        except ConnectionRequired:
            from agent_brain_client.interfaces.cli.access import run_address_install

            value = run_address_install(home, native, args.server, harness=args.harness, **key_options)
            from agent_brain_client.application.installation.follow_up import (
                installation_follow_up,
            )

            value = finish_result(installation_follow_up(value))
            return report(operation, value)
        except Exception:
            return report(
                operation,
                {
                    "status": "connection-file-review-required"
                    if args.connection
                    else "connection-recovery-review-required",
                    "installed": False,
                    "native_changed": False,
                    "credential_accessed": False,
                    "next_action": "Preserve connection metadata, original profile/policy and recovery receipts. Review the original binding; do not reset identity, replace digests or use an example connection.",
                },
            )
        if args.server is not None and endpoint_check(args.server) != endpoint_check(
            connection["endpoint"]
        ):
            raise ValueError("Existing Server identity preserved")
        value = run(home, native, connection, connection_sha, harness=args.harness)
        from agent_brain_client.application.installation.follow_up import (
            installation_follow_up,
        )

        value = finish_result(installation_follow_up(value))
        return report(operation, value)
    except RuntimeInUse as error:
        return report(operation, error.result())
    except Exception as error:
        from agent_brain_client.adapters.platforms.retention import RetentionFailure
        if fresh_requested and isinstance(error, RetentionFailure):
            from agent_brain_client.adapters.storage.reinstallation import read_journal
            record = read_journal(home)
            return report(operation, {
                "status": "fresh-install-archive-blocked", "installed": False,
                "native_changed": None, "stage": "archiving", "reason": type(error.__cause__).__name__,
                "transaction_id": record["id"], "retained_archive": record["archive"],
                "phase": record["phase"], "archived_items": record["archived"],
                "archive_error": error.details,
                "next_action": "Preserve this journal, archive and remaining source files. Review the exact operation/path and OS codes; access denied does not prove an ACL problem or a lock owner. Resolve only an independently identified blocker through its normal owner, then rerun the SAME reinstall command and Server/account to continue this transaction. Do not delete locks, change permissions/pins, replay queues or close all Clients. No new FRESH confirmation or re-archiving is needed for this unfinished transaction.",
            })
        return report(
            operation,
            {
                "status": "fresh-install-runtime-review-required"
                if args.action == "reinstall-check"
                else "fresh-install-review-or-resume-required"
                if fresh_requested
                else "first-install-review-or-resume-required",
                "installed": False,
                "native_changed": None if fresh_requested else False,
                **(
                    {
                        "next_action": "Run reinstall-check to identify exact busy leases. Disconnect only affected Agent Brain MCP services and allow active Hooks to finish, then rerun the SAME reinstall command. Preserve files and review configuration drift; do not kill processes, delete locks or edit pins.",
                        "stage": fresh_stage,
                        "reason": type(error).__name__,
                        **({"os_error": {"errno": error.errno, "winerror": getattr(error, "winerror", None),
                                          "failed_path": error.filename, "failed_path2": error.filename2}}
                           if isinstance(error, OSError) else {}),
                    }
                    if fresh_requested
                    else {}
                ),
            },
        )


if __name__ == "__main__":
    sys.exit(main())
