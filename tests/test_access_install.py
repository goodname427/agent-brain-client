"""Synthetic address/key flow and TLS transport; no real credential UI or data."""

import copy
from contextlib import nullcontext
import io
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
            bootstrap=Mock(return_value=copy.deepcopy(CONNECTION))
        )
        self.install = Mock(
            return_value={
                "status": "content-native-client-installed",
                "installed": True,
            }
        )
        self.prompts = Mock(return_value=KEY)
        self.platform = patch.object(
            access, "native_platform", return_value="darwin-arm64"
        )
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.grant_platform = patch(
            "brain_lock.native_platform", return_value="darwin-arm64"
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
        self.assertEqual(self.peer.bootstrap.call_args.args, (KEY, "darwin-arm64"))
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
        other = copy.deepcopy(CONNECTION)
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
            CONNECTION,
            grant=dict(CONNECTION["grant"], scopes=["read", "put", "remove"]),
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
