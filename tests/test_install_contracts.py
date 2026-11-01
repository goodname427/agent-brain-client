"""Portable metadata and native platform separation; no real devices."""

from contextlib import nullcontext
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / n) for n in ("tools", "scripts", "adapters/codex")]
from client_release import runtime_environment, UpdateBlocked
from https_content import DeviceEnrollment, grant_check, native_store
import prepare_install_runtime as bootstrap


class Contracts(unittest.TestCase):
    def test_windows_paths_spaces_drive_separators_and_no_ambient_environment(self):
        value = runtime_environment(
            r"C:\Program Files\Python\python.exe",
            r"D:\Git Tools\bin\git.exe",
            r"D:\Git Tools\libexec",
            windows=True,
        )
        self.assertEqual(value["PATH"], r"C:\Program Files\Python;D:\Git Tools\bin")
        self.assertEqual(value["GIT_EXEC_PATH"], r"D:\Git Tools\libexec")
        self.assertEqual(value["GIT_CONFIG_GLOBAL"], "NUL")
        self.assertEqual(value["GIT_TERMINAL_PROMPT"], "0")
        self.assertNotIn("PYTHONPATH", value)
        for path in (r"C:relative\python.exe", r"C:\tools\..\python.exe", "/relative"):
            with self.assertRaises(UpdateBlocked):
                runtime_environment(
                    path, r"C:\git\git.exe", r"C:\git\core", windows=True
                )

    def test_posix_paths_keep_spaces_and_distinct_separator(self):
        value = runtime_environment(
            "/opt/Python Tools/python3", "/opt/git/bin/git", "/opt/git/core"
        )
        self.assertEqual(value["PATH"], "/opt/Python Tools:/opt/git/bin")
        self.assertEqual(value["GIT_CONFIG_GLOBAL"], "/dev/null")

    def test_windows_contract_recovery_reuses_input_and_nonce_without_native_backend(
        self,
    ):
        grant = {
            "adapter": "codex",
            "platform": "win32-amd64",
            "actor": "synthetic",
            "scopes": ["read", "put"],
            "artifact_types": ["memory"],
            "ids": {"memory": "*"},
        }
        profile = {"grant": grant}

        class Store:
            record = None

            def lease(self):
                return nullcontext()

            def load(self):
                return copy.deepcopy(self.record)

            def save(self, value, expected):
                if expected != self.record:
                    raise ValueError("SYNTHETIC_CAS")
                self.record = copy.deepcopy(value)

        class Transport:
            seen = []
            lost = True

            def prepare(self, code, nonce, profile):
                self.seen.append((code, nonce))
                if self.lost:
                    self.lost = False
                    raise OSError("SYNTHETIC_LOST_REPLY")
                return {
                    "phase": "pending",
                    "nonce": nonce,
                    "credential": "t" * 40,
                    "device_id": "d" * 32,
                    "grant": profile["grant"],
                }

            def confirm(self, credential, nonce):
                return {"phase": "active", "device_id": "d" * 32, "grant": grant}

        store, transport = Store(), Transport()
        calls = []
        enrollment = DeviceEnrollment(
            profile, "a" * 64, store, transport, lambda: calls.append(True) or "i" * 40
        )
        with patch(
            "https_content.NativeKeychain",
            side_effect=AssertionError("No Mac backend in shared contract"),
        ):
            self.assertEqual(enrollment.run()["status"], "pairing-resume-required")
            self.assertEqual(enrollment.run()["status"], "device-paired")
        self.assertEqual(calls, [True])
        self.assertEqual(transport.seen[0], transport.seen[1])
        self.assertNotIn("code", store.record)
        # A synthetic injectable contract does not grant Windows production use.
        if sys.platform != "win32":
            with self.assertRaises(ValueError):
                grant_check(grant)
        with patch("sys.platform", "win32"):
            with self.assertRaises((ValueError, KeyError)):
                native_store(
                    {
                        "credential_helper": "synthetic.exe",
                        "credential_helper_sha256": "a" * 64,
                        "state_directory": "synthetic",
                    },
                    "a" * 64,
                ).backend.secure_input()


class PlatformMainContracts(unittest.TestCase):
    def config(self):
        from https_content import profile_metadata
        from client_release import (
            HTTPS_CONTENT_CAPABILITIES,
            WINDOWS_HTTPS_CONTENT_CLIENT_FILES,
        )

        grant = {
            "adapter": "codex",
            "platform": "win32-amd64",
            "actor": "synthetic-owner",
            "scopes": ["read", "put"],
            "artifact_types": ["memory", "rule", "skill"],
            "ids": {k: "personal-owner" for k in ("memory", "rule", "skill")},
        }
        home = r"C:\Users\synthetic"
        profile = profile_metadata(
            "https://synthetic.invalid",
            grant,
            {"kind": "windows-credential-manager", "source_sha256": "f" * 64},
            home + r"\state",
        )
        runtime = {
            "https_profile": home + r"\profile.json",
            "https_profile_sha256": "a" * 64,
            "content_policy": home + r"\content.json",
            "content_policy_sha256": "b" * 64,
            "credential_source_sha256": "f" * 64,
            "device_home": home,
            "codex_home": home + r"\codex",
            "content_cache": home + r"\content-cache",
        }
        policy = {
            "schema": 1,
            "adapter": "codex",
            "platform": "win32-amd64",
            "origin": "https://github.com/goodname427/agent-brain-client.git",
            "ref": "refs/heads/releases/personal-https-stable",
            "anchor_commit": "a" * 40,
            "channel": "stable",
            "core_api": 1,
            "state_schema": 1,
            "capabilities": HTTPS_CONTENT_CAPABILITIES,
            "file_prefixes": list(WINDOWS_HTTPS_CONTENT_CLIENT_FILES),
            "repository": home + r"\repository",
            "cache": home + r"\releases",
            "runtime": runtime,
        }
        mcp = {
            "command": r"C:\Python Tools\python.exe",
            "args": ["-I", "-B", home + r"\bootstrap\client_startup.py"],
            "env": runtime_environment(
                r"C:\Python Tools\python.exe",
                r"C:\Git Tools\git.exe",
                r"C:\Git Tools\core",
                windows=True,
            ),
        }
        plan = {
            "schema": 1,
            "adapter": "codex",
            "platform": "win32-amd64",
            "device_home": home,
            "codex_home": runtime["codex_home"],
            "package": home + r"\bootstrap",
            "package_sha256": "c" * 64,
            "release_policy": home + r"\release-policy.json",
            "release_policy_sha256": "d" * 64,
            "replace_existing": False,
            "mcp": mcp,
            "config_after_sha256": "e" * 64,
            "legacy_agents": None,
            "before": {
                runtime["codex_home"] + r"\config.toml": None,
                home + r"\.config\agent-brain-client\codex-mcp.json": None,
            },
        }
        return profile, policy, plan

    def test_shared_configuration_generates_windows_without_mac_helper_or_source_fields(
        self,
    ):
        from https_content import validate_profile_metadata
        from client_release import validate_release_policy
        from content_install import render, validate_install_scope
        import tomllib

        profile, policy, plan = self.config()
        self.assertEqual(
            validate_profile_metadata(json.loads(json.dumps(profile))), profile
        )
        self.assertEqual(validate_release_policy(policy), policy)
        self.assertNotIn("credential_helper", profile)
        self.assertEqual(policy["runtime"]["credential_source_sha256"], "f" * 64)
        home, native, paths = validate_install_scope(plan)
        self.assertEqual(str(home), plan["device_home"])
        self.assertEqual(len(paths), 2)
        rendered = tomllib.loads(
            render(b'model="synthetic"\n', plan["mcp"], False).decode()
        )
        self.assertEqual(rendered["model"], "synthetic")
        self.assertEqual(rendered["mcp_servers"]["agent_brain_server"], plan["mcp"])
        bad = copy.deepcopy(profile)
        bad["credential_helper"] = "synthetic.exe"
        with self.assertRaises(ValueError):
            validate_profile_metadata(bad)
        bad = copy.deepcopy(policy)
        bad["runtime"]["credential_source_sha256"] = "invalid"
        with self.assertRaises(UpdateBlocked):
            validate_release_policy(bad)
        bad = copy.deepcopy(plan)
        bad["before"][str(native / "unrelated.toml")] = None
        with self.assertRaises(ValueError):
            validate_install_scope(bad)

    def test_legacy_mac_metadata_generation_remains_byte_compatible(self):
        from https_content import profile_metadata, validate_profile_metadata

        grant = {
            "adapter": "codex",
            "platform": "darwin-arm64",
            "actor": "synthetic-owner",
            "scopes": ["read", "put"],
            "artifact_types": ["memory", "rule", "skill"],
            "ids": {k: "personal-owner" for k in ("memory", "rule", "skill")},
        }
        descriptor = {
            "kind": "mac-login-keychain",
            "helper": "/synthetic/helper",
            "helper_sha256": "a" * 64,
            "source_sha256": "b" * 64,
        }
        value = profile_metadata(
            "https://synthetic.invalid", grant, descriptor, "/synthetic/state"
        )
        expected = {
            "schema": 1,
            "transport": "https-system-credential",
            "adapter": "codex",
            "platform": "darwin-arm64",
            "data_mode": "personal",
            "endpoint": "https://synthetic.invalid",
            "grant": grant,
            "credential_helper": "/synthetic/helper",
            "credential_helper_sha256": "a" * 64,
            "credential_source_sha256": "b" * 64,
            "state_directory": "/synthetic/state",
        }
        self.assertEqual(value, expected)
        self.assertEqual(validate_profile_metadata(expected), expected)

    def test_shared_release_validation_preserves_permission_and_rollback_constraints(
        self,
    ):
        from brain_release import ReleaseEngine
        from client_release import validate_release_policy

        _, policy, _ = self.config()
        validate_release_policy(policy)
        engine = ReleaseEngine.__new__(ReleaseEngine)
        engine.policy = policy
        engine.migrations = {}
        manifest = {
            "schema": 1,
            "version": "0.2.5",
            "channel": "stable",
            "core_api": 1,
            "state_schema": 1,
            "python_min": "3.11",
            "capabilities": policy["capabilities"],
            "files": {k: "a" * 64 for k in policy["file_prefixes"]},
            "migrations": [],
        }
        engine.validate(manifest, {"version": "0.2.5"})
        for mutate in (
            lambda m: m.update(version="0.2.4"),
            lambda m: m.update(capabilities=m["capabilities"] + ["admin"]),
            lambda m: m.update(state_schema=2),
            lambda m: m.update(migrations=[{"id": "unreviewed"}]),
            lambda m: m["files"].update({"../outside.py": "a" * 64}),
        ):
            bad = copy.deepcopy(manifest)
            mutate(bad)
            with self.assertRaises(UpdateBlocked):
                engine.validate(bad, {"version": "0.2.5"})

    @unittest.skipIf(sys.platform == "win32", "Foreign-platform rejection; native Windows has separate integration coverage")
    def test_update_and_discovery_reject_before_fetch_activation_or_recovery(self):
        from brain_lock import PlatformUnsupported
        from client_release import ClientReleases
        from unittest.mock import Mock

        _, policy, _ = self.config()
        channel = ClientReleases.__new__(ClientReleases)
        channel.policy = policy
        channel.engine = Mock()
        self.assertEqual(channel.update()["status"], "platform-unsupported")
        self.assertEqual(
            channel.discover("codex", "win32-amd64", "0.2.5")["status"],
            "platform-unsupported",
        )
        with self.assertRaises(PlatformUnsupported):
            channel.verified_active()
        self.assertFalse(channel.engine.mock_calls)

    @unittest.skipIf(sys.platform == "win32", "Foreign-platform rejection; native Windows has separate integration coverage")
    def test_windows_install_and_recovery_never_mutate_existing_images(self):
        from brain_lock import PlatformUnsupported
        from content_install import apply, restore, inspect
        from unittest.mock import Mock

        _, _, plan = self.config()
        check = Mock()
        with (
            patch(
                "content_install.atomic",
                side_effect=AssertionError("No platform write"),
            ),
            patch("content_install.raw", side_effect=AssertionError("No private read")),
        ):
            result = apply(plan, "a" * 64, check)
            self.assertEqual(result["status"], "platform-unsupported")
            self.assertFalse(result["native_changed"])
            with self.assertRaises(PlatformUnsupported):
                restore({"schema": 1, "phase": "applying"}, plan)
            with patch("sys.platform", "win32"):
                self.assertEqual(
                    inspect(plan["device_home"])["status"], "platform-unsupported"
                )
        self.assertFalse(check.called)

    @unittest.skipIf(sys.platform == "win32", "Foreign-platform rejection; native Windows has separate integration coverage")
    def test_windows_permissions_cache_recovery_credentials_and_workers_fail_before_posix(
        self,
    ):
        from brain_lock import PlatformUnsupported, private_mode, sync_directory
        from content_cache import ContentAdapter
        from system_credentials import CredentialStore
        from client_release import policy_file
        from client_startup import serve_content
        from codex_connect import atomic

        profile, policy, plan = self.config()
        adapter = ContentAdapter.__new__(ContentAdapter)
        adapter.platform = "win32-amd64"
        with (
            patch("os.open", side_effect=AssertionError("No POSIX open")),
            patch("os.getuid", side_effect=AssertionError("No UID"), create=True),
            patch(
                "pathlib.Path.stat", side_effect=AssertionError("No ACL substitution")
            ),
        ):
            for call in (
                lambda: native_store(profile, "a" * 64),
                lambda: adapter._restore({}),
                lambda: adapter.refresh(),
            ):
                with self.assertRaises(PlatformUnsupported):
                    call()
            self.assertEqual(
                serve_content([], {"version": "0.2.5"}, policy, "current")["status"],
                "platform-unsupported",
            )
            with patch("sys.platform", "win32"):
                for call in (
                    lambda: private_mode("synthetic"),
                    lambda: sync_directory("synthetic"),
                    lambda: CredentialStore(None, "synthetic").lease(),
                    lambda: policy_file("synthetic", "a" * 64),
                    lambda: ContentAdapter("synthetic", "cache", {}, None),
                    lambda: atomic(Path("synthetic"), b"SYNTHETIC", 0o600),
                ):
                    with self.assertRaises(PlatformUnsupported):
                        call()


@unittest.skipIf(
    sys.platform == "win32",
    "Mac bootstrap provisioning only; portable contracts above run on Windows",
)
class Bootstrap(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()

    def archive(self, path, *, malicious=False):
        with tarfile.open(path, "w:gz") as out:
            entry = tarfile.TarInfo("python/bin/python3")
            if malicious:
                entry.type = tarfile.SYMTYPE
                entry.linkname = "../../../outside"
                out.addfile(entry)
            else:
                entry.mode = 0o700
                content = b"SYNTHETIC_UNEXECUTED_RUNTIME"
                entry.size = len(content)
                out.addfile(entry, io.BytesIO(content))
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_python_311_reuses_current_interpreter_without_download(self):
        with patch.object(bootstrap.sys, "version_info", (3, 11)):
            self.assertEqual(
                bootstrap.runtime(self.home, fetch=lambda p: self.fail("No download")),
                Path(sys.executable).resolve(),
            )
        self.assertFalse((self.home / ".config").exists())

    def test_verified_archive_preparation_reuses_runtime_and_retains_failed_staging(
        self,
    ):
        archive = self.home / "synthetic.tar.gz"
        digest = self.archive(archive)
        calls = []

        def fetch(path):
            calls.append(True)
            path.write_bytes(archive.read_bytes())

        with (
            patch.object(bootstrap.sys, "version_info", (3, 9)),
            patch.object(bootstrap, "DIGEST", digest),
        ):
            first = bootstrap.runtime(self.home, fetch=fetch)
            self.assertEqual(first.read_bytes(), b"SYNTHETIC_UNEXECUTED_RUNTIME")
            self.assertEqual(bootstrap.runtime(self.home, fetch=fetch), first)
            self.assertEqual(calls, [True])
            first.write_bytes(b"SYNTHETIC_DRIFT")
            with self.assertRaises(ValueError):
                bootstrap.runtime(self.home, fetch=fetch)

    def test_bad_digest_and_download_failure_never_execute_or_activate(self):
        with patch.object(bootstrap.sys, "version_info", (3, 9)):
            with self.assertRaises(ValueError):
                bootstrap.runtime(
                    self.home, fetch=lambda p: p.write_bytes(b"SYNTHETIC_CORRUPTION")
                )

            def unavailable(path):
                raise OSError("SYNTHETIC_OFFLINE")

            with self.assertRaises(OSError):
                bootstrap.runtime(self.home, fetch=unavailable)
        directory = self.home / ".config/agent-brain-client/install-runtime"
        self.assertFalse((directory / bootstrap.DIGEST).exists())
        self.assertTrue(list(directory.glob("preparing-*/python.tar.gz")))

    def test_verified_but_escaping_archive_is_rejected(self):
        archive = self.home / "synthetic.tar.gz"
        digest = self.archive(archive, malicious=True)
        with patch.object(bootstrap, "DIGEST", digest):
            with self.assertRaises(ValueError):
                bootstrap.extract(archive, self.home / "out")
        self.assertFalse((self.home / "out").exists())


if __name__ == "__main__":
    unittest.main()
