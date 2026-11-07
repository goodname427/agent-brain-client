"""Read-only event diagnosis using synthetic identities and mocked transports."""

import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_brain_client.domain.contract import validate
from agent_brain_client.adapters.server.collection import CollectionClient
from agent_brain_client.adapters.harnesses.codex.collection import status
from agent_brain_client.runtime import project_memory as project
from agent_brain_client.interfaces.cli import collection as cli


DIAGNOSTIC = {
    "state": "unknown",
    "reason": "event_not_found",
    "model_output_truth_verified": False,
}


class EventDiagnostics(unittest.TestCase):
    def test_contract_additive_and_status_only(self):
        base = {"project_id": "project-1", "session_id": "source-1"}
        validate("memory_status", base)
        validate("memory_status", dict(base, event_id="event:1"))
        for method in ("memory_resume", "memory_stop", "memory_sync", "memory_revoke"):
            with self.assertRaises(ValueError):
                validate(method, dict(base, event_id="event:1"))
        for bad in (None, "", "../event", "x" * 129, True):
            with self.assertRaises(ValueError):
                validate("memory_status", dict(base, event_id=bad))
        for extra in ({"device_id": "other"}, {"actor": "other"}, {"origin": {}}):
            with self.assertRaises(ValueError):
                validate("memory_status", dict(base, event_id="event:1", **extra))

    def test_project_uses_source_session_and_accepts_diagnostic_only_response(self):
        client = Mock()
        client.request.return_value = {"result": {"capture_extraction": DIAGNOSTIC}}
        with patch.object(
            project,
            "private_json",
            return_value={"project_id": "project-1", "recall_session_id": "reader-1"},
        ):
            result = project.daily_status(
                {"recall_policy": "fixture", "recall_policy_sha256": "pin"},
                client,
                event_id="event-1",
                session_id="source-1",
            )
        self.assertEqual(result["capture_extraction"], DIAGNOSTIC)
        request = client.request.call_args.args[0]
        self.assertEqual(request["method"], "memory_status")
        self.assertEqual(
            request["params"],
            {
                "project_id": "project-1",
                "session_id": "source-1",
                "event_id": "event-1",
            },
        )

    def test_project_old_server_and_errors_are_not_success(self):
        client = Mock()
        for response in (
            {"error": {"code": "denied"}},
            {"result": {"schema": 2}},
            {"result": {"capture_extraction": None}},
        ):
            client.request.return_value = response
            with patch.object(
                project,
                "private_json",
                return_value={"project_id": "p", "recall_session_id": "r"},
            ):
                with self.assertRaises(ValueError):
                    project.daily_status(
                        {"recall_policy": "fixture", "recall_policy_sha256": "pin"},
                        client,
                        event_id="e",
                        session_id="s",
                    )

    def test_original_failure_and_unverified_truth_are_preserved(self):
        diagnostic = dict(
            DIAGNOSTIC,
            state="continued",
            reason="operator_continued",
            original_state="review-required",
            original_completed=False,
        )
        client = Mock()
        client.request.return_value = {"result": {"capture_extraction": diagnostic}}
        with patch.object(
            project,
            "private_json",
            return_value={"project_id": "p", "recall_session_id": "r"},
        ):
            result = project.daily_status(
                {"recall_policy": "fixture", "recall_policy_sha256": "pin"},
                client,
                event_id="e",
                session_id="s",
            )
        self.assertEqual(result["capture_extraction"], diagnostic)
        self.assertFalse(result["capture_extraction"]["model_output_truth_verified"])

    def test_project_diagnostics_do_not_open_outbox(self):
        client = Mock()
        with (
            patch.object(project, "daily", return_value=True),
            patch.object(project, "adapter_for", side_effect=AssertionError("outbox")),
            patch(
                "agent_brain_client.adapters.server.https.HTTPSContentClient",
                return_value=client,
            ),
            patch.object(project, "daily_status", return_value=DIAGNOSTIC) as read,
        ):
            project.daily_command(
                {"profile": "fixture", "profile_sha256": "pin"},
                "server-status",
                event_id="e",
                session_id="s",
            )
            read.assert_called_once_with(
                {"profile": "fixture", "profile_sha256": "pin"},
                client,
                event_id="e",
                session_id="s",
            )

    def test_project_rejects_invalid_or_mutating_query_before_io(self):
        with patch.object(project, "adapter_for", side_effect=AssertionError("IO")):
            for args in (
                {"event_id": "e"},
                {"session_id": "s"},
                {"event_id": "../e", "session_id": "s"},
            ):
                with self.assertRaises(ValueError):
                    project.daily_command({}, "server-status", **args)
            with self.assertRaises(ValueError):
                project.daily_command({}, "resume", event_id="e", session_id="s")

    def test_global_exact_origin_no_metadata_admission_probe(self):
        local = {"kind": "non-project", "cwd": "synthetic"}
        client = Mock()
        client.status.return_value = {"capture_extraction": DIAGNOSTIC}
        services = SimpleNamespace(
            eligible=Mock(return_value=local),
            CollectionClient=Mock(return_value=client),
        )
        result = status(
            {},
            check_server=True,
            event_id="event-1",
            session_id="source-1",
            services=services,
        )
        services.CollectionClient.assert_called_once_with({}, local)
        client.status.assert_called_once_with("source-1", event_id="event-1")
        client.wire.assert_not_called()
        self.assertEqual(
            result["server_event_status"]["capture_extraction"], DIAGNOSTIC
        )

    def test_global_rejects_unapproved_origin_and_incomplete_identity(self):
        services = SimpleNamespace(
            eligible=Mock(return_value=None), CollectionClient=Mock()
        )
        for args in (
            {"event_id": "e"},
            {"session_id": "s"},
            {"event_id": "e", "session_id": "s"},
        ):
            with self.assertRaises(ValueError):
                status({}, services=services, **args)
        services.CollectionClient.assert_not_called()

    def test_global_transport_preserves_scope_validation(self):
        client = object.__new__(CollectionClient)
        client.value = {"scope": {"profile_id": "profile"}}
        client.scope_sha = "pin"
        client.params = Mock(
            return_value={"origin": {"kind": "project"}, "session_id": "source-1"}
        )
        response = {
            "global_profile_id": "profile",
            "scope_sha256": "pin",
            "capture_extraction": DIAGNOSTIC,
        }
        client.wire = Mock(return_value={"result": response})
        self.assertEqual(client.status("source-1", event_id="event-1"), response)
        client.wire.assert_called_once_with(
            "memory_global_status",
            {
                "origin": {"kind": "project"},
                "session_id": "source-1",
                "event_id": "event-1",
            },
        )
        response["scope_sha256"] = "other"
        with self.assertRaises(ValueError):
            client.status("source-1", event_id="event-1")

    def test_cli_requires_pair_and_read_only_action(self):
        for argv in (
            ["global-status", "--event-id", "e"],
            ["global-status", "--session-id", "s"],
            ["global-resume", "--event-id", "e", "--session-id", "s"],
            [
                "global-status",
                "--event-id",
                "e",
                "--session-id",
                "s",
                "--request-key",
                "r",
            ],
        ):
            with (
                patch.object(cli.core, "load", side_effect=AssertionError("IO")),
                patch("sys.stderr", io.StringIO()),
            ):
                with self.assertRaises(SystemExit):
                    cli.main(argv)

    def test_cli_passes_source_identity(self):
        with (
            patch.object(cli.core, "load", return_value={}) as load,
            patch.object(cli.core, "status", return_value={}) as read,
            patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(
                cli.main(["global-status", "--event-id", "e", "--session-id", "s"]), 0
            )
        self.assertFalse(load.call_args.kwargs["verify_code"])
        read.assert_called_once_with(
            {}, check_server=False, request_key=None, event_id="e", session_id="s"
        )


class DiagnosticCliFailures(unittest.TestCase):
    def test_global_safe_unavailable_reason(self):
        import json
        from agent_brain_client.domain.errors import EventDiagnosticsUnavailable

        for error, reason in (
            (
                EventDiagnosticsUnavailable("private details"),
                "server-diagnostics-unsupported",
            ),
            (ValueError("secret token"), "diagnostics-unconfirmed"),
        ):
            output = io.StringIO()
            with (
                patch.object(cli.core, "load", return_value={}),
                patch.object(cli.core, "status", side_effect=error),
                patch("sys.stdout", output),
            ):
                self.assertEqual(
                    cli.main(["global-status", "--event-id", "e", "--session-id", "s"]),
                    2,
                )
            result = json.loads(output.getvalue())
            self.assertEqual(result["reason"], reason)
            self.assertNotIn(str(error), output.getvalue())

    def test_project_safe_unavailable_reason(self):
        import json
        from agent_brain_client.interfaces.cli import project as cli_project
        from agent_brain_client.domain.errors import EventDiagnosticsUnavailable

        for error, reason in (
            (
                EventDiagnosticsUnavailable("private details"),
                "server-diagnostics-unsupported",
            ),
            (ValueError("secret token"), "diagnostics-unconfirmed"),
        ):
            output = io.StringIO()
            with (
                patch(
                    "sys.argv",
                    [
                        "project",
                        "server-status",
                        "--settings",
                        "fixture",
                        "--settings-sha256",
                        "pin",
                        "--event-id",
                        "e",
                        "--session-id",
                        "s",
                    ],
                ),
                patch.object(cli_project, "load", return_value={}),
                patch.object(cli_project, "daily_command", side_effect=error),
                patch("sys.stdout", output),
            ):
                self.assertEqual(cli_project.main(), 2)
            result = json.loads(output.getvalue())
            self.assertEqual(result["reason"], reason)
            self.assertNotIn(str(error), output.getvalue())
