"""Synthetic repository first-install transactions; no real UI, Keychain, issuer or server."""

import copy
import hashlib
import io
import json
import os
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
from client_release import dependency_environment
from system_credentials import canonical
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
        dependencies = self.home / "tools"
        dependencies.mkdir(mode=0o700)
        python, git = dependencies / "python3", dependencies / "git"
        core = dependencies / "git-core"
        core.mkdir()
        for path in (python, git, core / "git-remote-https"):
            path.write_bytes(b"#!/bin/sh\n# SYNTHETIC_UNEXECUTED_DEPENDENCY\nexit 0\n")
            path.chmod(0o700)
        helper = dependencies / "credential-helper"
        helper.write_bytes(b"#!/bin/sh\n# SYNTHETIC_UNEXECUTED_HELPER\nexit 0\n")
        helper.chmod(0o700)
        self.materials = {
            "source_sha256": hashlib.sha256(
                (ROOT / "native/CredentialStore.swift").read_bytes()
            ).hexdigest(),
            "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
            "credential_helper": str(helper),
            "dependencies": {
                "schema": 2,
                "python": str(python),
                "git": str(git),
                "git_exec_path": str(core),
                "python_sha256": hashlib.sha256(python.read_bytes()).hexdigest(),
                "git_sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
            },
        }
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
        (self.home / ".config/agent-brain-client/first-install").mkdir(
            parents=True, mode=0o700
        )
        self.native = self.home / "codex"
        self.native.mkdir(mode=0o700)
        self.config = self.native / "config.toml"
        self.config.write_text('model="synthetic"\n')
        self.config.chmod(0o600)
        self.events = []
        self.runtime = patch.object(
            sys, "executable", self.materials["dependencies"]["python"]
        )
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.backend.confirm_reset = lambda *args: True
        return dependencies

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
            "dependency_preparer": lambda *a, **k: self.materials,
            "store": self.store,
            "transport": self.transport,
            "secure_input": self.input,
            "installer": lambda p, d: content_install.apply(p, d, lambda value: None),
        }
        options.update(kwargs)
        return wizard.run(
            self.home,
            self.native,
            self.connection,
            self.connection_sha,
            **options,
        )

    def test_first_install_cli_emits_onboard_handoff_without_importing(self):
        self.fixture()
        original = wizard.run

        def run(home, native, connection, digest):
            return original(home, native, connection, digest,
                            confirm=lambda *a: 'Continue', updater=self.update,
                            runner=self.runner, dependency_preparer=lambda *a, **k: self.materials,
                            store=self.store, transport=self.transport, secure_input=self.input,
                            installer=lambda p, d: content_install.apply(p, d, lambda value: None))

        output = io.StringIO()
        with patch.object(Path, 'home', return_value=self.home), \
                patch.dict(os.environ, CODEX_HOME=str(self.native)), \
                patch.object(sys, 'argv', ['mac_first_install.py', '--connection', str(self.base / 'connection.json')]), \
                patch.object(wizard, 'run', side_effect=run), patch.object(sys, 'stdout', output):
            code = wizard.main()
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0, result)
        self.assertTrue(result['installed'])
        self.assertEqual(result['model_loaded'], 'unknown')
        self.assertEqual(result['onboard_check']['status'], 'coverage-check-required')
        self.assertFalse(result['onboard_check']['source_scan_performed'])
        self.assertFalse(result['onboard_check']['data_transferred'])


    def test_required_tools_and_helper_are_prepared_once_and_drift_is_preserved(self):
        self.fixture()
        builds = []

        def helper_builder(path):
            builds.append(path)
            path.write_bytes(b"SYNTHETIC_COMPILED_HELPER")
            path.chmod(0o700)
            return {
                "helper_sha256": wizard.sha(path.read_bytes()),
                "source_sha256": self.materials["source_sha256"],
            }

        def runner(argv, **kwargs):
            if "--exec-path" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    self.materials["dependencies"]["git_exec_path"].encode(),
                    b"",
                )
            return self.runner(argv, **kwargs)

        with patch.object(
            wizard.shutil, "which", return_value=self.materials["dependencies"]["git"]
        ):
            prepared = wizard.prepare_dependencies(
                self.home, runner=runner, helper_builder=helper_builder
            )
            self.assertEqual(
                wizard.prepare_dependencies(
                    self.home, runner=runner, helper_builder=helper_builder
                ),
                prepared,
            )
            self.assertEqual(len(builds), 1)
            builds[0].write_bytes(b"SYNTHETIC_DRIFT")
            with self.assertRaises(ValueError):
                wizard.prepare_dependencies(
                    self.home, runner=runner, helper_builder=helper_builder
                )
        self.assertEqual(builds[0].read_bytes(), b"SYNTHETIC_DRIFT")
        self.assertEqual(self.prompts, 0)

    def test_first_install_prepared_environment_and_exact_native_transaction(
        self,
    ):
        self.fixture()
        result = self.run_wizard()
        self.assertTrue(result["installed"], result)
        state = self.home / ".config/agent-brain-client/first-install"
        ready = content_install.private_json(next(state.glob("prior-ready-*.json")))
        self.assertEqual(ready["status"], "first-install-ready")
        self.assertFalse(ready["credential_accessed"])
        self.assertFalse(ready["native_changed"])
        plan = content_install.private_json(state / "install-plan.json")
        self.assertIn(str(self.home / "tools"), plan["mcp"]["env"]["PATH"])
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
        self.assertFalse(
            (self.home / ".config/agent-brain-client/first-install/ready.json").exists()
        )
        plan_path = (
            self.home / ".config/agent-brain-client/first-install/install-plan.json"
        )
        plan_before = plan_path.read_bytes()
        self.assertTrue(self.run_wizard()["installed"])
        self.assertEqual(plan_path.read_bytes(), plan_before)
        self.assertEqual(self.prompts, 1)
        self.assertEqual(self.transport.prepares, [(saved["code"], saved["nonce"])] * 2)
        self.assertEqual(sum(event[0] == "update" for event in self.events), 1)
        self.assertFalse(
            (self.home / ".config/agent-brain-client/first-install/ready.json").exists()
        )

    def test_native_drift_and_dependency_drift_stop_before_secure_input(self):
        self.fixture()
        decisions = iter(["Continue", "Back"])
        self.run_wizard(confirm=lambda *args: next(decisions))
        self.config.write_text('external_change="synthetic"\n')
        self.assertFalse(self.run_wizard()["installed"])
        self.assertEqual(self.prompts, 0)
        self.assertEqual(self.config.read_text(), 'external_change="synthetic"\n')
        Path(self.materials["dependencies"]["git"]).write_bytes(b"SYNTHETIC_DRIFT")
        self.assertFalse(self.run_wizard()["installed"])
        self.assertEqual(self.prompts, 0)
        self.assertIsNone(self.backend.record)

    def test_connection_and_existing_installation_preserved(self):
        self.fixture()
        registration = self.home / ".config/agent-brain-client/codex-mcp.json"
        registration.write_text("SYNTHETIC_EXISTING_REGISTRATION")
        registration.chmod(0o600)
        before = registration.read_bytes()
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
            lambda v: v["release"].update(ref="refs/heads/releases/personal-stable"),
            lambda v: v["grant"].update(scopes=["read", "put", "remove"]),
        ):
            value = copy.deepcopy(self.connection)
            mutate(value)
            path.write_bytes(canonical(value))
            with self.assertRaises(ValueError):
                wizard.connection_file(path)

    def test_dependency_failure_never_inputs_invitation(self):
        self.fixture()

        def failed(*args, **kwargs):
            raise ValueError("SYNTHETIC_UNAVAILABLE_TOOLS")

        self.assertFalse(self.run_wizard(dependency_preparer=failed)["installed"])
        self.assertEqual(self.prompts, 0)
        self.assertFalse(self.transport.prepares)

    def test_old_readiness_is_preserved_but_invalidated_before_failed_recheck(self):
        self.fixture()
        decisions = iter(["Continue", "Back"])
        self.run_wizard(confirm=lambda *args: next(decisions))
        state = self.home / ".config/agent-brain-client/first-install"
        previous = (state / "ready.json").read_bytes()

        def failure(*a, **k):
            raise ValueError("SYNTHETIC_UNAVAILABLE_DEPENDENCY")

        self.assertFalse(self.run_wizard(dependency_preparer=failure)["installed"])
        self.assertFalse((state / "ready.json").exists())
        self.assertEqual(next(state.glob("prior-ready-*.json")).read_bytes(), previous)
        self.assertEqual(self.prompts, 0)

    def test_duplicate_launch_does_not_prompt_or_modify_transaction(self):
        self.fixture()
        lock = self.home / ".config/agent-brain-client/first-install/first-install.lock"
        with wizard.private_lease(lock.parent):
            result = self.run_wizard(confirm=lambda *args: self.fail("Duplicate UI"))
        self.assertEqual(result["status"], "first-install-in-progress")
        self.assertEqual(self.prompts, 0)

    def test_interrupted_bootstrap_export_is_preserved_and_retry_uses_complete_package(
        self,
    ):
        self.fixture()
        actual = wizard.export

        def interrupted(source, output, **kwargs):
            output.mkdir()
            (output / "partial").write_bytes(b"SYNTHETIC_PARTIAL")
            raise OSError("SYNTHETIC_INTERRUPTION")

        with patch.object(wizard, "export", side_effect=interrupted):
            self.assertFalse(self.run_wizard()["installed"])
        state = self.home / ".config/agent-brain-client/first-install"
        evidence = next(state.glob("bootstrap-*/package/partial"))
        self.assertEqual(self.prompts, 0)
        with patch.object(wizard, "export", side_effect=actual):
            self.assertTrue(self.run_wizard()["installed"])
        self.assertEqual(evidence.read_bytes(), b"SYNTHETIC_PARTIAL")

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
            "tool_contract_version": 1,
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

    def test_readiness_expiry_uses_one_clock_snapshot(self):
        self.fixture()
        decisions = iter(["Continue", "Back"])
        with patch.object(wizard.time, "time", side_effect=[2000, 2001]) as clock:
            self.assertEqual(
                self.run_wizard(confirm=lambda *args: next(decisions))["status"],
                "first-install-ready",
            )
        ready = content_install.private_json(
            self.home / ".config/agent-brain-client/first-install/ready.json"
        )
        self.assertEqual(clock.call_count, 1)
        self.assertEqual(ready["valid_until"] - ready["prepared_at"], 600)

    def test_ready_receipts_reuse_installation_identity_across_restart_and_bind_drift(
        self,
    ):
        self.fixture()
        state = self.home / ".config/agent-brain-client/first-install"

        def ready_run():
            decisions = iter(["Continue", "Back"])
            return self.run_wizard(confirm=lambda *args: next(decisions))

        self.assertEqual(ready_run()["status"], "first-install-ready")
        first = content_install.private_json(state / "ready.json")
        self.assertEqual(first["schema"], 2)
        self.assertEqual(ready_run()["status"], "first-install-ready")
        second = content_install.private_json(state / "ready.json")
        self.assertEqual(first["installation_id"], second["installation_id"])
        identity = state / "installation-id.json"
        content_install.save(identity, {"schema": 1, "id": "9" * 32})
        self.assertFalse(ready_run()["installed"])
        self.assertEqual(self.prompts, 0)

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


class LifecycleExitCodes(unittest.TestCase):
    def command(self,action,response):
        output=io.StringIO()
        with patch.object(sys,'argv',['mac_first_install.py','--action',action]),patch.object(sys,'platform','darwin'),patch.object(wizard.platform,'machine',return_value='arm64'),patch.object(wizard.Path,'home',return_value=Path('/synthetic/home')),patch('client_lifecycle.'+action,return_value=response),patch.object(sys,'stdout',output):
            code=wizard.main()
        self.assertEqual(json.loads(output.getvalue()),response)
        return code

    def test_uninstall_cancel_and_pending_are_not_success_even_if_installed(self):
        for status in ('uninstall-cancelled','uninstall-unconfirmed-writes','client-in-use'):
            with self.subTest(status=status):
                self.assertEqual(self.command('uninstall',{'status':status,'installed':True,'native_changed':False}),2)
        for status in ('client-uninstalled','client-not-installed'):
            self.assertEqual(self.command('uninstall',{'status':status,'installed':False,'native_changed':False}),0)

    def test_status_and_repair_do_not_confuse_review_with_absent_installation(self):
        self.assertEqual(self.command('status',{'status':'content-native-install-review-required','installed':False,'installation_present':True,'reason':'mcp-binding-changed','native_changed':False}),2)
        self.assertEqual(self.command('status',{'status':'content-native-client-installed','installed':True,'native_changed':False}),0)
        self.assertEqual(self.command('repair',{'status':'uninstall-recovery-review-required','installed':False,'native_changed':None}),2)
        paused={'status':'content-native-client-disabled','installed':False,'registration_verified':True,'native_changed':False}
        self.assertEqual(self.command('status',paused),0)
        self.assertEqual(self.command('uninstall',paused),2)
        self.assertEqual(self.command('status',{'status':'uninstall-state-review-required','installed':False,'native_changed':False,'uninstall_verified':False}),2)


class NewDeviceConnectionCLI(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin' and wizard.platform.machine().lower() == 'arm64', 'Actual Mac arm64 CLI only')
    def test_actual_process_requests_local_address_before_preparing_or_pairing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            env = dict(os.environ, HOME=str(home), CODEX_HOME=str(home / 'codex'))
            result = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'tools/mac_first_install.py')],
                                    env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2, result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(value['status'], 'access-input-cancelled')
            self.assertIn('HTTPS address', result.stderr)
            self.assertFalse(value['credential_accessed'])
            self.assertFalse((home / '.config').exists())

    def test_orphan_saved_description_is_not_a_trusted_identity(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            path = home / '.config/agent-brain-client/first-install/connection.json'
            content_install.save(path, {'schema': 1})
            with patch.object(Path, 'home', return_value=home), \
                    patch.object(sys, 'argv', ['mac_first_install.py']), \
                    patch.object(sys, 'stdout', io.StringIO()) as output, \
                    patch.object(wizard, 'run', side_effect=AssertionError('Must not install')):
                self.assertEqual(wizard.main(), 2)
            self.assertEqual(json.loads(output.getvalue())['status'], 'connection-recovery-review-required')


if __name__ == "__main__":
    unittest.main()
