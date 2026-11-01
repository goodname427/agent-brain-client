"""Synthetic address/key flow and TLS transport; no real credential UI or data."""

import copy
from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, MagicMock, patch
from urllib.error import HTTPError

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1] / p)
    for p in ("tools", "adapters/codex", "scripts", "tests")
]
import access_install as access
import mac_first_install as wizard
import test_mac_first_install as fixtures
from test_https_content import Backend
from system_credentials import canonical

KEY = "SYNTHETIC_ACCESS_KEY_12345678901234567890123456789"
CONNECTION = {
    "schema": 1,
    "endpoint": "https://synthetic.invalid",
    "grant": {
        "actor": "synthetic-owner",
        "adapter": "codex",
        "platform": "darwin-arm64",
        "scopes": ["read", "put"],
        "artifact_types": ["memory", "rule", "skill"],
        "ids": {k: "personal-owner" for k in ("memory", "rule", "skill")},
    },
    "release": {
        "origin": wizard.OFFICIAL_ORIGIN,
        "ref": wizard.OFFICIAL_REF,
        "anchor_commit": "a" * 40,
    },
}


class AddressInstall(unittest.TestCase):
    def setUp(self):
        from brain_lock import native_platform

        self.target = native_platform()
        self.connection = copy.deepcopy(CONNECTION)
        self.connection["grant"]["platform"] = self.target
        if self.target.startswith("win32-"):
            self.connection["release"]["ref"] = (
                "refs/heads/releases/personal-https-windows-stable"
            )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "home"
        self.home.mkdir(mode=0o700)
        self.native = self.home / "codex"
        self.native.mkdir(mode=0o700)
        self.backend = Backend()
        self.store = SimpleNamespace(
            load=self.backend.load,
            save=self.backend.save,
            clear=self.backend.clear,
            lease=nullcontext,
        )
        self.peer = SimpleNamespace(
            bootstrap=Mock(return_value=copy.deepcopy(self.connection))
        )
        self.install = Mock(
            return_value={
                "status": "content-native-client-installed",
                "installed": True,
            }
        )
        self.prompts = Mock(return_value=KEY)
        self.platform = patch.object(
            access, "native_platform", return_value=self.target
        )
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.grant_platform = patch(
            "brain_lock.native_platform", return_value=self.target
        )
        self.grant_platform.start()
        self.addCleanup(self.grant_platform.stop)

    def run_install(self, endpoint="https://synthetic.invalid", **kwargs):
        options = dict(
            transport=self.peer,
            store_factory=lambda *a: self.store,
            dependency_preparer=lambda *a: {},
            install=self.install,
            key_input=self.prompts,
        )
        options.update(kwargs)
        return access.run_address_install(self.home, self.native, endpoint, **options)

    def test_only_address_and_hidden_key_no_public_secret_and_no_false_success(self):
        self.assertTrue(self.run_install()["installed"])
        self.prompts.assert_called_once()
        self.assertEqual(self.backend.record["key"], KEY)
        self.assertTrue(self.install.call_args.kwargs["access_mode"])
        self.assertEqual(self.peer.bootstrap.call_args.args, (KEY, self.target))
        self.assertNotIn(
            KEY.encode(),
            b"".join(p.read_bytes() for p in self.home.rglob("*") if p.is_file()),
        )
        self.install.return_value = {
            "status": "https-install-review-or-resume-required",
            "installed": False,
        }
        self.assertFalse(self.run_install(None)["installed"])
        self.prompts.assert_called_once()

    def test_network_failure_preserves_saved_key_but_explicit_rejection_can_retry(self):
        self.peer.bootstrap.side_effect = OSError("Synthetic network loss")
        self.assertFalse(self.run_install()["installed"])
        self.install.assert_not_called()
        saved = copy.deepcopy(self.backend.record)
        self.assertFalse(self.run_install(None)["installed"])
        self.assertEqual(self.backend.record, saved)
        self.prompts.assert_called_once()
        self.peer.bootstrap.side_effect = access.AccessDenied("Synthetic rejection")
        self.assertEqual(self.run_install(None)["status"], "access-key-rejected")
        self.assertIsNone(self.backend.record)
        self.peer.bootstrap.side_effect = None
        self.assertTrue(self.run_install(None)["installed"])
        self.assertEqual(self.prompts.call_count, 2)

    def test_cancel_identity_change_and_unverified_legacy_material_preserved(self):
        self.assertEqual(
            self.run_install(key_input=lambda *a: None)["status"],
            "access-input-cancelled",
        )
        self.assertIsNone(self.backend.record)
        self.install.assert_not_called()
        self.assertFalse(
            self.run_install("https://other.synthetic.invalid")["installed"]
        )
        self.prompts.assert_not_called()
        other = copy.deepcopy(self.connection)
        other["endpoint"] = "https://other.synthetic.invalid"
        self.peer.bootstrap.return_value = other
        self.assertFalse(self.run_install()["installed"])
        self.install.assert_not_called()
        marker = self.home / ".config/agent-brain-client/access-bootstrap/endpoint.json"
        marker.unlink()
        self.assertFalse(self.run_install()["installed"])
        self.install.assert_not_called()

    def test_bootstrap_precedes_install_and_no_install_after_bad_metadata(self):
        self.peer.bootstrap.return_value = dict(
            self.connection,
            grant=dict(self.connection["grant"], scopes=["read", "put", "remove"]),
        )
        self.assertFalse(self.run_install()["installed"])
        self.install.assert_not_called()
        self.assertEqual(self.backend.record["key"], KEY)


class AccessTransport(unittest.TestCase):
    def peer(self, response=None, error=None):
        peer = access.AccessHTTPS("https://synthetic.invalid")
        opener = MagicMock()
        if error:
            opener.open.side_effect = error
        else:
            opener.open.return_value.__enter__.return_value = SimpleNamespace(
                status=200, read=lambda limit: canonical(response)
            )
        peer.opener = opener
        return peer, opener

    def test_authorization_only_and_selected_origin_bound(self):
        peer, opener = self.peer({"schema": 1, "connection": CONNECTION})
        with patch.object(wizard, "connection_value", side_effect=lambda c: c):
            self.assertEqual(peer.bootstrap(KEY, "darwin-arm64"), CONNECTION)
        request = opener.open.call_args.args[0]
        self.assertEqual(
            request.full_url, "https://synthetic.invalid/v1/access/bootstrap"
        )
        self.assertEqual(request.headers["Authorization"], "Bearer " + KEY)
        self.assertNotIn(KEY.encode(), request.data)
        peer, _ = self.peer(
            {
                "schema": 1,
                "connection": dict(
                    CONNECTION, endpoint="https://other.synthetic.invalid"
                ),
            }
        )
        with (
            patch.object(wizard, "connection_value", side_effect=lambda c: c),
            self.assertRaises(ValueError),
        ):
            peer.bootstrap(KEY, "darwin-arm64")

    def test_no_redirect_tls_error_or_unknown_rejection_retires_key(self):
        peer = access.AccessHTTPS("https://synthetic.invalid")
        from remote_http import NoRedirect

        self.assertTrue(any(isinstance(h, NoRedirect) for h in peer.opener.handlers))
        for status, code, expected in (
            (302, "access_denied", ValueError),
            (403, "proxy_error", ValueError),
            (403, "access_denied", access.AccessDenied),
        ):
            error = HTTPError(
                "https://synthetic.invalid",
                status,
                "Synthetic",
                {},
                io.BytesIO(canonical({"error": {"code": code}})),
            )
            peer, _ = self.peer(error=error)
            with self.assertRaises(expected) as caught:
                peer.access("bootstrap", {}, KEY)
            self.assertEqual(
                isinstance(caught.exception, access.AccessDenied),
                expected is access.AccessDenied,
            )
        import ssl

        peer, _ = self.peer(
            error=ssl.SSLCertVerificationError("Synthetic untrusted certificate")
        )
        with self.assertRaises(ValueError) as caught:
            peer.access("bootstrap", {}, KEY)
        self.assertNotIsInstance(caught.exception, access.AccessDenied)


@unittest.skipIf(
    os.name == "nt", "Mac native transaction fixture; shared flow above runs on Windows"
)
class AccessTransaction(unittest.TestCase):
    def test_real_local_transaction_with_lost_prepare_resume_and_no_invitation_ui(self):
        fixture = fixtures.FirstInstall(
            "test_first_install_cli_emits_onboard_handoff_without_importing"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fixture()
        # Legacy transaction itself is exercised; access admission is tested at
        # the Server boundary and cross-repo integration, not reimplemented here.
        fixture.transport.lost_prepare = True
        first = fixture.run_wizard(
            access_mode=True,
            confirm=lambda *a: self.fail("No additional invitation/scope input"),
            secure_input=lambda: fixture.code,
        )
        self.assertFalse(first["installed"])
        second = fixture.run_wizard(
            access_mode=True,
            confirm=lambda *a: self.fail("No additional invitation/scope input"),
            secure_input=lambda: self.fail("Recovery must reuse record"),
        )
        self.assertTrue(second["installed"], second)
        device_id = fixture.backend.record["device_id"]
        with patch(
            "content_install.inspect",
            return_value={"installed": True, "native_changed": False},
        ):
            third = fixture.run_wizard(
                access_mode=True, confirm=lambda *a: self.fail("No repeated prompt")
            )
        self.assertTrue(third["installed"], third)
        self.assertEqual(fixture.backend.record["device_id"], device_id)


class WindowsAccessDialog(unittest.TestCase):
    def test_generic_hidden_key_prompt_no_os_auth_or_automatic_persistence(self):
        import ctypes as C
        from access_windows import secure_input

        captured = []

        def prompt(
            info,
            target,
            context,
            error,
            username,
            namesize,
            password,
            size,
            save,
            flags,
        ):
            self.assertEqual(info._obj.cbSize, C.sizeof(info._obj))
            self.assertIn("https://synthetic.invalid", info._obj.pszMessageText)
            self.assertEqual(username.value, "Agent Brain access key")
            self.assertEqual(flags, 0x140082)
            self.assertFalse(save._obj.value)
            self.assertIsNone(context)
            self.assertEqual(error, 0)
            password.value = KEY
            captured.append(password)
            return 0

        self.assertEqual(secure_input("https://synthetic.invalid", prompt=prompt), KEY)
        self.assertEqual(captured[0].value, "")

    def test_cancel_error_and_invalid_input_clear_buffers_without_fallback(self):
        from access_windows import secure_input

        for status, value in ((1223, KEY), (5, KEY), (0, "invalid")):
            captured = []

            def prompt(
                info,
                target,
                context,
                error,
                username,
                namesize,
                password,
                size,
                save,
                flags,
            ):
                password.value = value
                captured.append(password)
                return status

            if status == 1223:
                self.assertIsNone(
                    secure_input("https://synthetic.invalid", prompt=prompt)
                )
            else:
                with self.assertRaises(ValueError):
                    secure_input("https://synthetic.invalid", prompt=prompt)
            self.assertEqual(captured[0].value, "")

    @unittest.skipUnless(
        sys.platform == "win32", "Native Windows CredUI symbol and ABI"
    )
    def test_native_dialog_api_binding_without_opening_ui(self):
        import ctypes as C
        from access_windows import dialog_api, CredUIInfo

        prompt = dialog_api()
        self.assertEqual(len(prompt.argtypes), 10)
        self.assertEqual(C.sizeof(CredUIInfo), 40 if C.sizeof(C.c_void_p) == 8 else 20)


class BootstrapCacheEntry(unittest.TestCase):
    setUp = AddressInstall.setUp

    def test_fresh_checkout_cache_reaches_address_and_key_input_without_legacy_file(
        self,
    ):
        import bootstrap_main
        import subprocess

        def git(args, **kwargs):
            if "clone" in args:
                (Path(args[-1]) / ".git").mkdir()
                return subprocess.CompletedProcess(args, 0, "", "")
            if "rev-parse" in args:
                return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
            if "branch" in args:
                return subprocess.CompletedProcess(args, 0, "main\n", "")
            if "remote" in args:
                return subprocess.CompletedProcess(
                    args, 0, bootstrap_main.ORIGIN + "\n", ""
                )
            if "status" in args:
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(args)

        cache = self.home / ".config/agent-brain-client/installer-checkouts"
        checkout = bootstrap_main.fresh_checkout(self.home, runner=git, root=cache)
        address = Mock(return_value="https://synthetic.invalid")
        original = access.run_address_install

        def install(home, native, endpoint=None):
            return original(
                home,
                native,
                endpoint,
                transport=self.peer,
                store_factory=lambda *a: self.store,
                dependency_preparer=lambda *a: {},
                key_input=lambda *a: None,
                address_input=address,
                install=self.install,
            )

        with (
            patch.object(Path, "home", return_value=self.home),
            patch.dict(os.environ, CODEX_HOME=str(self.native)),
            patch.object(sys, "argv", ["mac_first_install.py"]),
            patch.object(sys, "stdout", io.StringIO()) as output,
            patch.object(access, "run_address_install", side_effect=install),
        ):
            self.assertEqual(wizard.main(), 2)
        self.assertEqual(
            json.loads(output.getvalue())["status"], "access-input-cancelled"
        )
        address.assert_called_once()
        self.assertTrue((checkout / ".git/agent-brain-source-sha").is_file())


@unittest.skipIf(
    os.name == "nt",
    "Mac transaction fixture; Windows native transaction case is separate",
)
class ApplyingAccessEntry(unittest.TestCase):
    def fixture(self):
        import content_install

        f = fixtures.FirstInstall(
            "test_first_install_cli_emits_onboard_handoff_without_importing"
        )
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.fixture()
        self.assertTrue(f.run_wizard(access_mode=True)["installed"])
        state = f.home / ".config/agent-brain-client"
        receipt = wizard.private_json(state / "content-install-receipt.json")
        receipt["phase"] = "applying"
        wizard.save(state / "content-install-receipt.json", receipt)
        wizard.same_private(
            state / "access-bootstrap/endpoint.json",
            {"schema": 1, "endpoint": f.connection["endpoint"]},
        )
        wizard.same_private(state / "access-bootstrap/connection.json", f.connection)
        self.assertFalse(content_install.inspect(f.home).get("registration_verified"))
        return f, state, receipt

    def test_cli_resumes_after_registration_write_without_another_key_or_identity(self):
        import content_install

        f, state, _ = self.fixture()
        identity = copy.deepcopy(f.backend.record)
        original = wizard.run

        def run(home, native, connection, digest, **options):
            return original(
                home,
                native,
                connection,
                digest,
                **options,
                confirm=lambda *a: self.fail("No new consent/invitation prompt"),
                updater=lambda: self.fail("No release/identity replacement"),
                runner=f.runner,
                dependency_preparer=lambda *a, **k: f.materials,
                store=f.store,
                secure_input=lambda: self.fail("No new key input"),
                installer=lambda p, d: content_install.apply(p, d, lambda v: None),
            )

        with (
            patch.object(Path, "home", return_value=f.home),
            patch.dict(os.environ, CODEX_HOME=str(f.native)),
            patch.object(sys, "argv", ["mac_first_install.py"]),
            patch.object(wizard, "run", side_effect=run),
            patch.object(sys, "stdout", io.StringIO()) as output,
        ):
            self.assertEqual(wizard.main(), 0, output.getvalue())
        self.assertTrue(json.loads(output.getvalue())["installed"])
        self.assertEqual(
            wizard.private_json(state / "content-install-receipt.json")["phase"],
            "complete",
        )
        self.assertEqual(f.backend.record, identity)

    def test_cli_preserves_applying_journal_and_outside_edit_before_credential_access(
        self,
    ):
        f, state, _ = self.fixture()
        f.config.write_text('model="synthetic"\n# external edit must survive\n')
        before = f.config.read_bytes()
        journal = (state / "content-install-receipt.json").read_bytes()
        with (
            patch.object(Path, "home", return_value=f.home),
            patch.dict(os.environ, CODEX_HOME=str(f.native)),
            patch.object(sys, "argv", ["mac_first_install.py"]),
            patch.object(
                wizard,
                "run",
                side_effect=AssertionError("No recovery over outside edits"),
            ),
            patch.object(sys, "stdout", io.StringIO()) as output,
        ):
            self.assertEqual(wizard.main(), 2)
        self.assertFalse(json.loads(output.getvalue())["installed"])
        self.assertEqual(f.config.read_bytes(), before)
        self.assertEqual((state / "content-install-receipt.json").read_bytes(), journal)
