"""Bounded startup diagnostics never expose exception values or credentials."""
import io
import json
import ssl
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters" / "codex"))

import client_release
import client_startup
from brain_release import UpdateBlocked
from brain_lock import LockTimeout, locked
import brain_lock
import types
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


    def test_tls_and_http_errors_are_classified_before_oserror(self):
        cases = (
            (
                "identity-activation",
                ssl.SSLError("SYNTHETIC_TLS_SECRET"),
                "SSLError",
            ),
            (
                "installation-check",
                HTTPError(
                    "https://synthetic.invalid",
                    503,
                    "SYNTHETIC_HTTP_SECRET",
                    {},
                    io.BytesIO(b"SYNTHETIC_HTTP_BODY"),
                ),
                "HTTPError",
            ),
        )
        for stage, error, error_class in cases:
            with self.subTest(error_class=error_class):
                output = io.StringIO()
                code = emit_startup_failure(stage, error, output)
                self.assertEqual(code, 1)
                self.assertEqual(
                    json.loads(output.getvalue()),
                    {
                        "stage": stage,
                        "error_class": error_class,
                        "error_code": "startup_transport_error",
                    },
                )
                self.assertNotIn("SYNTHETIC_", output.getvalue())

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

    def test_normal_startup_emits_bounded_release_diagnostic(self):
        diagnostic = {
            "stage": "release-check",
            "error_class": "OSError",
            "error_code": "release-check-failed",
        }
        releases = mock.Mock()
        releases.update.return_value = {
            "status": "release-review-required",
            "active": {"version": "0.2.13"},
            "diagnostic": diagnostic,
        }
        releases.verified_active.return_value = {
            "version": "0.2.13",
            "package": "/synthetic/package",
        }
        releases.policy = {
            "platform": "synthetic-platform",
            "capabilities": ["codex-stdio-mcp", "personal-memory-explicit"],
            "runtime": {
                "endpoint": "https://synthetic.invalid",
                "token_file": "/synthetic/token",
            },
        }
        output = io.StringIO()
        with (
            mock.patch.object(client_startup, "ClientReleases", return_value=releases),
            mock.patch.object(client_startup, "content_runtime_blocker", return_value=None),
            mock.patch.object(
                client_startup.subprocess, "run", return_value=mock.Mock(returncode=0)
            ),
            mock.patch("sys.stderr", output),
        ):
            result = client_startup.startup("synthetic-policy", "synthetic-digest")

        self.assertTrue(result["served"])
        self.assertEqual(result["status"], "release-review-required")
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertIn(diagnostic, lines)
        self.assertNotIn("SYNTHETIC_SECRET", output.getvalue())

    def test_locked_timeout_raises_specific_lock_timeout(self):
        fake = types.ModuleType("fcntl")
        fake.LOCK_SH = 1
        fake.LOCK_EX = 2
        fake.LOCK_NB = 4
        fake.flock = mock.Mock(side_effect=OSError("SYNTHETIC_LOCK_CONTENTION"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.lock"
            with (
                mock.patch.dict(sys.modules, {"fcntl": fake}),
                mock.patch.object(brain_lock.time, "monotonic", side_effect=[0, 21]),
            ):
                with self.assertRaises(LockTimeout):
                    with locked(path, timeout=20):
                        self.fail("lock should time out before entering")

    def test_lock_timeout_never_reads_live_pointer_and_fails_closed(self):
        releases = object.__new__(client_release.ClientReleases)
        releases.policy = {"platform": "synthetic-platform"}
        releases.inspection = False
        releases.engine = mock.Mock()
        releases.engine.check.side_effect = LockTimeout(
            "SYNTHETIC_SECRET_LOCK_CONTENTION_DETAIL"
        )
        # This would be an unhealth-checked candidate if read during activation.
        releases.verified_active = mock.Mock(return_value={
            "version": "0.2.14",
            "package": "/synthetic/unhealth-checked-candidate",
        })

        with mock.patch.object(client_release, "require_platform"):
            result = client_release.ClientReleases.update(releases)

        self.assertEqual(result["status"], "release-review-required")
        self.assertIsNone(result["active"])
        releases.verified_active.assert_not_called()
        self.assertEqual(
            result["diagnostic"],
            {
                "stage": "release-check",
                "error_class": "LockTimeout",
                "error_code": "release-lock-busy",
            },
        )
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))

        # Startup must not serve when no previously verified package was retained.
        releases_for_startup = mock.Mock()
        releases_for_startup.update.return_value = result
        output = io.StringIO()
        with (
            mock.patch.object(client_startup, "ClientReleases", return_value=releases_for_startup),
            mock.patch("sys.stderr", output),
        ):
            startup_result = client_startup.startup("synthetic-policy", "synthetic-digest")
        self.assertFalse(startup_result["served"])
        self.assertEqual(startup_result["status"], "release-review-required")

        releases.engine.check.side_effect = RuntimeError("SYNTHETIC_OTHER_RUNTIME")
        with mock.patch.object(client_release, "require_platform"):
            with self.assertRaisesRegex(RuntimeError, "SYNTHETIC_OTHER_RUNTIME"):
                client_release.ClientReleases.update(releases)

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
