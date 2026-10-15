"""Bounded startup diagnostics never expose exception values or credentials."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters" / "codex"))

import client_release
from brain_release import UpdateBlocked
import content_mcp
from content_mcp import emit_startup_failure


class StartupDiagnostics(unittest.TestCase):
    def test_candidate_failure_is_redacted_and_remains_closed(self):
        output = io.StringIO()
        code = emit_startup_failure(
            "identity-activation",
            RuntimeError("SYNTHETIC_SECRET_POLICY_AND_PROFILE_TEXT"),
            output,
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "stage": "identity-activation",
                "error_class": "RuntimeError",
                "error_code": "startup_blocked",
            },
        )
        self.assertNotIn("SYNTHETIC_SECRET", output.getvalue())


    def test_oserror_subclasses_are_reported_as_safe_io_category(self):
        output = io.StringIO()
        code = emit_startup_failure(
            "content-policy-validation",
            FileNotFoundError("SYNTHETIC_SECRET_PATH"),
            output,
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "stage": "content-policy-validation",
                "error_class": "OSError",
                "error_code": "startup_io_error",
            },
        )
        self.assertNotIn("SYNTHETIC_SECRET", output.getvalue())

    def test_invalid_arguments_do_not_echo_untrusted_values(self):
        output = io.StringIO()
        argv = ["content_mcp.py", "--unknown-option", "SYNTHETIC_SECRET_ARGUMENT"]
        with mock.patch.object(sys, "argv", argv), mock.patch("sys.stderr", output):
            with self.assertRaises(SystemExit) as failure:
                content_mcp.main()
        self.assertEqual(failure.exception.code, 1)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "stage": "arguments",
                "error_class": "ValueError",
                "error_code": "startup_validation_error",
            },
        )
        self.assertNotIn("SYNTHETIC_SECRET", output.getvalue())

    def test_release_failure_keeps_review_gate_and_only_adds_safe_diagnostic(self):
        releases = object.__new__(client_release.ClientReleases)
        releases.policy = {"platform": "synthetic-platform"}
        releases.inspection = False
        releases.engine = mock.Mock()
        releases.engine.check.side_effect = UpdateBlocked(
            "SYNTHETIC_SECRET_POLICY_AND_PACKAGE_DETAIL"
        )
        releases.verified_active = lambda: {"version": "0.2.13"}

        with mock.patch.object(client_release, "require_platform"):
            result = client_release.ClientReleases.update(releases)

        self.assertEqual(result["status"], "release-review-required")
        self.assertEqual(result["active"], {"version": "0.2.13"})
        self.assertEqual(
            result["diagnostic"],
            {
                "stage": "release-check",
                "error_class": "UpdateBlocked",
                "error_code": "release-policy-blocked",
            },
        )
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
