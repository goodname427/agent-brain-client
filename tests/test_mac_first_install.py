"""Synthetic first-install payloads; no real UI, Keychain, issuer or server."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1] / name) for name in ("tools", "tests")
]
import test_https_content as fixtures

GRANT, ROOT = fixtures.GRANT, fixtures.ROOT
from client_pairing import EnrollmentTerminal
from client_release import dependency_environment, tree_files, UpdateBlocked
from system_credentials import canonical
from build_mac_installer import build
import mac_first_install as wizard
import content_install


class FirstInstall(unittest.TestCase):
    def setUp(self):
        fixtures.HTTPSContent.setUp(self)
        self.machine = patch("platform.machine", return_value="arm64")
        self.machine.start()
        self.addCleanup(self.machine.stop)
        self.fixture_grant = copy.deepcopy(GRANT)
        self.fixture_grant["platform"] = "darwin-arm64"
        self.code = self.authority.issue(self.fixture_grant, 300)

    input = fixtures.HTTPSContent.input

    def fixture(self):
        python = self.base / "synthetic-python"
        git = self.base / "synthetic-git"
        for root, executable in (
            (python, "bin/python3"),
            (git, "bin/git"),
            (git, "libexec/git-core/git-remote-https"),
        ):
            path = root / executable
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"#!/bin/sh\n# SYNTHETIC_UNEXECUTED_DEPENDENCY\nexit 0\n")
            path.chmod(0o700)
        for root in (python, git):
            (root / "LICENSE").write_text("Synthetic test-only license fixture\n")
        python_link = python / "bin/python3.12"
        python_link.symlink_to("python3")
        provenance = self.base / "provenance.json"
        provenance.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "platform": "darwin-arm64",
                    **{
                        name: {
                            "source": "https://synthetic.invalid/" + name,
                            "archive_sha256": "a" * 64,
                            "license_files": ["LICENSE"],
                        }
                        for name in ("python", "git")
                    },
                }
            )
        )

        def helper_builder(path):
            path.write_bytes(b"#!/bin/sh\n# SYNTHETIC_UNEXECUTED_HELPER\nexit 0\n")
            path.chmod(0o700)
            return {
                "source_sha256": hashlib.sha256(
                    (ROOT / "native/CredentialStore.swift").read_bytes()
                ).hexdigest(),
                "helper_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "self_check": {
                    "credential_helper_self_check": "passed",
                    "keychain_accessed": False,
                    "ui_opened": False,
                },
            }

        def probe(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {"platform": "Darwin-arm64", "version": [3, 12], "tls": True}
                ).encode(),
                b"",
            )

        app = self.base / "Synthetic.app"
        report = build(
            app, python, git, provenance, helper_builder=helper_builder, probe=probe
        )
        self.assertFalse(report["installed"])
        self.assertFalse(report["published"])
        self.assertFalse(report["keychain_accessed"])
        original = app / "Contents/Resources"
        bundle = wizard.copy_payload(original, self.home)
        self.assertEqual(wizard.copy_payload(original, self.home), bundle)
        self.connection = {
            "schema": 1,
            "endpoint": "https://synthetic.invalid",
            "grant": copy.deepcopy(self.fixture_grant),
            "release": {
                "origin": wizard.OFFICIAL_ORIGIN,
                "ref": wizard.OFFICIAL_REF,
                "anchor_commit": "a" * 40,
            },
        }
        connection_path = self.base / "connection.json"
        connection_path.write_bytes(canonical(self.connection))
        self.connection, self.connection_sha = wizard.connection_file(connection_path)
        self.bundle = bundle
        self.native = self.home / "codex"
        self.native.mkdir(mode=0o700)
        self.config = self.native / "config.toml"
        self.config.write_text('model="synthetic"\n')
        self.config.chmod(0o600)
        self.events = []
        self.runtime = patch.object(
            sys, "executable", str(bundle / "dependencies/python/bin/python3")
        )
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.backend.confirm_reset = lambda *args: True
        return original

    def runner(self, argv, **kwargs):
        self.events.append(("process", argv))
        if "--self-check" in argv:
            stdout = json.dumps(
                {
                    "credential_helper_self_check": "passed",
                    "keychain_accessed": False,
                    "ui_opened": False,
                }
            ).encode()
        elif "get-url" in argv:
            stdout = wizard.OFFICIAL_ORIGIN.encode()
        elif "init" in argv:
            (Path(argv[-1]) / ".git").mkdir(parents=True)
            stdout = b""
        else:
            stdout = b""
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    def update(self):
        self.events.append(("update", None))
        self.assertEqual(self.prompts, 0)
        self.assertIsNone(self.backend.record)
        return {"status": "updated", "active": {"version": "0.2.6"}}

    def run_wizard(self, **kwargs):
        options = {
            "confirm": lambda *args: "Continue",
            "updater": self.update,
            "runner": self.runner,
            "store": self.store,
            "transport": self.transport,
            "secure_input": self.input,
            "installer": lambda p, d: content_install.apply(p, d, lambda value: None),
        }
        options.update(kwargs)
        return wizard.run(
            self.bundle,
            self.home,
            self.native,
            self.connection,
            self.connection_sha,
            **options,
        )

    def test_first_install_bundled_environment_readiness_and_exact_native_transaction(
        self,
    ):
        self.fixture()
        result = self.run_wizard()
        self.assertTrue(result["installed"], result)
        state = self.home / ".config/agent-brain-client/first-install"
        ready = content_install.private_json(state / "ready.json")
        self.assertEqual(ready["status"], "first-install-ready")
        self.assertFalse(ready["credential_accessed"])
        self.assertFalse(ready["native_changed"])
        plan = content_install.private_json(state / "install-plan.json")
        self.assertIn("dependencies/git/bin", plan["mcp"]["env"]["PATH"])
        self.assertIn('model="synthetic"', self.config.read_text())
        self.assertEqual(self.prompts, 1)
        self.assertEqual(sum(event[0] == "update" for event in self.events), 1)
        for path in state.rglob("*"):
            if path.is_file() and not path.is_symlink():
                self.assertNotIn(self.code.encode(), path.read_bytes())
                self.assertNotIn(
                    self.backend.record["credential"].encode(), path.read_bytes()
                )
        with patch.object(
            content_install,
            "inspect",
            return_value={"installed": True, "native_changed": False},
        ):
            self.assertTrue(self.run_wizard()["installed"])
        self.assertEqual(self.prompts, 1)

    def test_cancel_server_review_and_back_after_ready_never_prompts_or_pairs(self):
        self.fixture()
        before = self.config.read_bytes()
        self.assertEqual(
            self.run_wizard(confirm=lambda *args: "Cancel")["status"],
            "first-install-cancelled",
        )
        self.assertFalse(self.events)
        self.assertFalse(self.transport.prepares)
        decisions = iter(["Continue", "Back"])
        self.assertEqual(
            self.run_wizard(confirm=lambda *args: next(decisions))["status"],
            "first-install-ready",
        )
        self.assertEqual(self.config.read_bytes(), before)
        self.assertIsNone(self.backend.record)
        self.assertEqual(self.prompts, 0)

    def test_release_network_failure_never_declares_ready_or_requests_invitation(self):
        self.fixture()
        before = self.config.read_bytes()
        result = self.run_wizard(
            updater=lambda: {"status": "release-review-required", "active": None}
        )
        self.assertFalse(result["installed"])
        self.assertFalse(
            (self.home / ".config/agent-brain-client/first-install/ready.json").exists()
        )
        self.assertIsNone(self.backend.record)
        self.assertEqual(self.prompts, 0)
        self.assertEqual(self.config.read_bytes(), before)

    def test_lost_prepare_reply_restart_keeps_nonce_code_and_plan(self):
        self.fixture()
        self.transport.lost_prepare = True
        first = self.run_wizard()
        self.assertEqual(first["status"], "pairing-resume-required")
        saved = copy.deepcopy(self.backend.record)
        plan_path = (
            self.home / ".config/agent-brain-client/first-install/install-plan.json"
        )
        plan_before = plan_path.read_bytes()
        self.assertTrue(self.run_wizard()["installed"])
        self.assertEqual(plan_path.read_bytes(), plan_before)
        self.assertEqual(self.prompts, 1)
        self.assertEqual(self.transport.prepares, [(saved["code"], saved["nonce"])] * 2)
        self.assertEqual(sum(event[0] == "update" for event in self.events), 1)

    def test_native_drift_and_dependency_drift_stop_before_secure_input(self):
        self.fixture()
        decisions = iter(["Continue", "Back"])
        self.run_wizard(confirm=lambda *args: next(decisions))
        self.config.write_text('external_change="synthetic"\n')
        self.assertFalse(self.run_wizard()["installed"])
        self.assertEqual(self.prompts, 0)
        self.assertEqual(self.config.read_text(), 'external_change="synthetic"\n')
        (self.bundle / "dependencies/git/bin/git").write_bytes(b"SYNTHETIC_DRIFT")
        self.assertFalse(self.run_wizard()["installed"])
        self.assertEqual(self.prompts, 0)
        self.assertIsNone(self.backend.record)

    def test_connection_and_existing_installation_preserved(self):
        original = self.fixture()
        registration = self.home / ".config/agent-brain-client/codex-mcp.json"
        registration.write_text("SYNTHETIC_EXISTING_REGISTRATION")
        registration.chmod(0o600)
        before = registration.read_bytes()
        with self.assertRaises(ValueError):
            wizard.copy_payload(original, self.home)
        self.assertFalse(self.run_wizard()["installed"])
        self.assertEqual(registration.read_bytes(), before)
        self.assertEqual(self.prompts, 0)

    def test_connection_rejects_credentials_scope_and_release_authority_drift(self):
        self.fixture()
        path = self.base / "bad-connection.json"
        for mutate in (
            lambda v: v.update(token="SYNTHETIC_TOKEN"),
            lambda v: v.update(endpoint="https://user:password@synthetic.invalid"),
            lambda v: v["release"].update(
                origin="https://synthetic.invalid/client.git"
            ),
            lambda v: v["grant"].update(scopes=["read", "put", "remove"]),
        ):
            value = copy.deepcopy(self.connection)
            mutate(value)
            path.write_bytes(canonical(value))
            with self.assertRaises(ValueError):
                wizard.connection_file(path)

    def test_external_symlinks_unrecognized_files_and_link_drift_are_rejected(self):
        self.fixture()
        (self.bundle / "unrecognized.txt").write_text("SYNTHETIC_EXTRA")
        with self.assertRaises(ValueError):
            wizard.bundle_file(self.bundle)
        root = self.base / "unsafe-runtime"
        root.mkdir()
        (root / "external").symlink_to("/usr/bin/git")
        with self.assertRaises(UpdateBlocked):
            tree_files(root)

    def test_duplicate_launch_does_not_prompt_or_modify_transaction(self):
        self.fixture()
        lock = self.home / ".config/agent-brain-client/first-install/first-install.lock"
        with wizard.locked(lock, timeout=0):
            result = self.run_wizard(confirm=lambda *args: self.fail("Duplicate UI"))
        self.assertEqual(result["status"], "first-install-in-progress")
        self.assertEqual(self.prompts, 0)

    def test_interrupted_payload_copy_retains_staging_and_resumes_missing_file(self):
        original = self.fixture()
        state = self.home / ".config/agent-brain-client/first-install"
        staging = state / "payload-staging"
        staging.mkdir(mode=0o700, exist_ok=True)
        interrupted = staging / "payload-interrupted-fixture"
        interrupted.write_bytes(b"SYNTHETIC_PARTIAL_COPY")
        missing = self.bundle / "dependencies/git/bin/git"
        missing.unlink()
        self.assertEqual(wizard.copy_payload(original, self.home), self.bundle)
        self.assertEqual(interrupted.read_bytes(), b"SYNTHETIC_PARTIAL_COPY")
        self.assertTrue(missing.is_file())
        wizard.bundle_file(self.bundle)

    def test_native_protocol_probe_uses_pinned_dependencies_without_ambient_injection(
        self,
    ):
        self.fixture()
        decisions = iter(["Continue", "Back"])
        self.run_wizard(confirm=lambda *args: next(decisions))
        plan = content_install.private_json(
            self.home / ".config/agent-brain-client/first-install/install-plan.json"
        )
        reply = {
            "status": "content-native-protocol-checked",
            "protocol_checked": True,
            "authentication_checked": True,
            "native_changed": False,
            "tools": plan["mcp"]["enabled_tools"],
        }
        with patch.object(
            content_install.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, canonical(reply), b""),
        ) as process:
            content_install.probe(plan)
        environment = process.call_args.kwargs["env"]
        self.assertEqual(environment["PATH"], plan["mcp"]["env"]["PATH"])
        self.assertEqual(environment["HOME"], str(self.home))
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertNotIn("DYLD_INSERT_LIBRARIES", environment)

    def test_dependency_environment_is_optional_for_existing_policies(self):
        self.assertIsNone(dependency_environment({"runtime": {}}))


class PreparingReset(unittest.TestCase):
    setUp = fixtures.HTTPSContent.setUp
    coordinator = fixtures.HTTPSContent.coordinator
    input = fixtures.HTTPSContent.input

    def test_explicit_prepare_denial_can_be_cancelled_then_cleared_without_new_identity(
        self,
    ):
        def denied(*args):
            raise EnrollmentTerminal("pairing_prepare_rejected")

        self.transport.prepare = denied
        result = self.coordinator().run()
        self.assertEqual(result["reason"], "pairing_prepare_rejected")
        before = copy.deepcopy(self.backend.record)
        self.assertEqual(
            self.coordinator().reset_terminal(lambda *args: False)["status"],
            "pairing-reset-cancelled",
        )
        self.assertEqual(self.backend.record, before)
        self.assertEqual(
            self.coordinator().reset_terminal(lambda *args: True)["status"],
            "pairing-reenrollment-ready",
        )
        self.assertIsNone(self.backend.record)
        self.assertEqual(self.prompts, 1)

    def test_network_error_or_successful_prepare_cannot_clear_saved_recovery(self):
        self.transport.lost_prepare = True
        self.coordinator().run()
        before = copy.deepcopy(self.backend.record)
        self.transport.prepare = lambda *args: (_ for _ in ()).throw(
            OSError("SYNTHETIC_NETWORK_FAILURE")
        )
        self.assertEqual(
            self.coordinator().reset_terminal(
                lambda *args: self.fail("No reset UI on network failure")
            )["status"],
            "pairing-reset-blocked",
        )
        self.assertEqual(self.backend.record, before)
        self.transport.prepare = lambda *args: {"phase": "pending"}
        self.assertEqual(
            self.coordinator().reset_terminal(
                lambda *args: self.fail("No reset UI on success")
            )["status"],
            "pairing-reset-blocked",
        )
        self.assertEqual(self.backend.record, before)

    def test_bad_format_does_not_create_saved_preparing_record(self):
        coordinator = self.coordinator()
        coordinator.secure_input = lambda: "wrong"
        self.assertEqual(coordinator.run()["status"], "pairing-invalid-input")
        self.assertIsNone(self.backend.record)
        self.assertFalse(self.transport.prepares)

    def test_wire_prepare_denied_terminal_and_unknown_errors_retain_recovery(self):
        from urllib.error import HTTPError
        import io
        from https_content import PairingHTTPS

        transport = PairingHTTPS("https://synthetic.invalid")

        class Fail:
            code = "pairing_denied"
            status = 403

            def open(inner, request, **kwargs):
                raise HTTPError(
                    request.full_url,
                    inner.status,
                    "SYNTHETIC",
                    {},
                    io.BytesIO(
                        json.dumps(
                            {"error": {"code": inner.code, "debug": self.code}}
                        ).encode()
                    ),
                )

        opener = Fail()
        transport.opener = opener
        with self.assertRaises(EnrollmentTerminal) as caught:
            transport.prepare(self.code, "n" * 32, self.profile)
        self.assertEqual(caught.exception.reason, "pairing_prepare_rejected")
        for status, code in (
            (500, "pairing_denied"),
            (403, "unknown"),
            (401, "pairing_denied"),
        ):
            opener.status, opener.code = status, code
            with self.assertRaises(ValueError):
                transport.prepare(self.code, "n" * 32, self.profile)


if __name__ == "__main__":
    unittest.main()
