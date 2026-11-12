"""Fresh reconnection with synthetic retained memories; no real host or credential."""

from contextlib import nullcontext
import copy
import io as streamio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_brain_client.domain import contract
from agent_brain_client.adapters.storage import metadata as io
from agent_brain_client.adapters.storage.host_files import atomic
from agent_brain_client.adapters.platforms.filesystem import (
    private_directory,
    native_platform,
    locked,
)
from agent_brain_client.adapters.packages.client import HTTPS_CONTENT_CAPABILITIES
from agent_brain_client.adapters.installation.installation import (
    registered_mcp_digest,
)
from agent_brain_client.adapters.server.https import profile_metadata
from agent_brain_client.application.installation.fresh import (
    prepare_fresh_installation,
    verify_memory_access,
)
from agent_brain_client.runtime.fresh_installation import retained_installation
from agent_brain_client.adapters.storage.reinstallation import read_journal

MEMORY = {
    "title": "Retained preference",
    "description": "Synthetic existing memory",
    "body": "Keep the existing remembered fact.",
    "type": "user",
}


class FreshInstallation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "home"
        self.native = self.home / ".codex"
        self.root = self.home / ".config/agent-brain-client"
        self.cache = self.root / "first-install/content-cache"
        for directory in (
            self.home,
            self.native,
            self.cache,
            self.root / "first-install",
            self.root / "content/memory",
        ):
            private_directory(directory)
        self.grant = {
            "adapter": "codex",
            "platform": native_platform(),
            "actor": "synthetic-owner",
            "scopes": ["read", "put"],
            "artifact_types": ["memory", "rule", "skill"],
            "ids": {k: "personal-owner" for k in ("memory", "rule", "skill")},
        }
        self.content = {
            "schema": 1,
            "adapter": "codex",
            "kinds": self.grant["artifact_types"],
            "ids": self.grant["ids"],
            "dependencies": {"commands": [], "python_modules": []},
        }
        self.old_profile = {
            "endpoint": "https://synthetic.invalid",
            "grant": self.grant,
            "old_pin": "a" * 64,
        }
        self.profile = self.root / "first-install/https-profile.json"
        io.write(self.profile, self.old_profile)
        self.old_profile_sha = io.digest(self.profile.read_bytes())
        io.write(self.root / "first-install/content-policy.json", self.content)
        self.policy = {
            "capabilities": HTTPS_CONTENT_CAPABILITIES,
            "file_prefixes": ["old-25-file-layout"],
            "runtime": {
                "device_home": str(self.home),
                "codex_home": str(self.native),
                "content_cache": str(self.cache),
                "https_profile": str(self.profile),
                "https_profile_sha256": self.old_profile_sha,
                "content_policy": str(self.root / "first-install/content-policy.json"),
                "content_policy_sha256": io.digest(
                    (self.root / "first-install/content-policy.json").read_bytes()
                ),
            },
        }
        io.write(self.root / "first-install/release-policy.json", self.policy)
        self.mcp = {
            "command": "/synthetic/unexecuted-python",
            "args": ["old-entry.py"],
            "enabled": True,
        }
        self.config = self.native / "config.toml"
        atomic(
            self.config,
            b'model = "preserved"\n[mcp_servers.other]\ncommand = "other"\n[mcp_servers.agent_brain_server]\ncommand = "/synthetic/unexecuted-python"\nargs = ["old-entry.py"]\nenabled = true\n',
            0o600,
        )
        self.metadata = {
            "schema": 1,
            "mcp_sha256": registered_mcp_digest(self.mcp),
            "release_policy": str(self.root / "first-install/release-policy.json"),
            "release_policy_sha256": io.digest(
                (self.root / "first-install/release-policy.json").read_bytes()
            ),
        }
        io.write(self.root / "codex-mcp.json", self.metadata)
        self.memory_path = self.root / "content/memory/personal-owner.md"
        atomic(self.memory_path, MEMORY["body"].encode(), 0o600)
        self.binding = {"device_home": str(self.home), "codex_home": str(self.native)}
        self.owned = {
            "policy_sha": io.digest(
                contract.encode({"policy": self.content, "binding": self.binding})
            ),
            "binding": self.binding,
            "files": {
                self.memory_path.relative_to(self.home).as_posix(): io.digest(
                    self.memory_path.read_bytes()
                )
            },
        }
        io.write(self.cache / "owned.json", self.owned)
        private_directory(self.cache / "outbox")
        self.old_queue = self.cache / "outbox/retained_original_request.json"
        atomic(self.old_queue, b"SYNTHETIC_UNSENT_OLD_QUEUE_DO_NOT_REPLAY", 0o600)
        self.native_memory = self.native / "memories/existing.md"
        private_directory(self.native_memory.parent)
        atomic(self.native_memory, b"SYNTHETIC_NATIVE_MEMORY_NOT_UPLOADED", 0o600)
        self.port = retained_installation(self.home, self.native)

    def prepare(self, confirm=lambda _: "FRESH"):
        with self.port.lease():
            return prepare_fresh_installation(self.port, confirm)

    def test_retains_old_files_and_queue_restores_only_memory_projection(self):
        before = self.profile.read_bytes()
        result = self.prepare()
        self.assertEqual(result["status"], "fresh-install-ready")
        archive = Path(result["archive"])
        self.assertEqual(
            (archive / "first-install/https-profile.json").read_bytes(), before
        )
        self.assertEqual(
            (
                archive
                / "first-install/content-cache/outbox/retained_original_request.json"
            ).read_bytes(),
            b"SYNTHETIC_UNSENT_OLD_QUEUE_DO_NOT_REPLAY",
        )
        self.assertFalse(self.profile.exists())
        self.assertNotIn("agent_brain_server", self.config.read_text())
        self.assertIn("[mcp_servers.other]", self.config.read_text())
        self.port.restore_mapping(self.cache, self.content)
        self.assertEqual(self.memory_path.read_text(), MEMORY["body"])
        self.assertFalse((self.cache / "outbox").exists())
        self.assertFalse((self.cache / "imports.json").exists())
        self.assertEqual(
            self.native_memory.read_bytes(), b"SYNTHETIC_NATIVE_MEMORY_NOT_UPLOADED"
        )
        self.assertEqual(self.prepare()["archive"], result["archive"])

    def test_cancel_and_mapping_drift_never_detach_or_remove_data(self):
        before = self.config.read_bytes()
        self.assertEqual(
            self.prepare(lambda _: "")["status"], "fresh-install-cancelled"
        )
        self.assertIsNone(read_journal(self.home))
        self.assertEqual(self.config.read_bytes(), before)

        def edit(_):
            atomic(self.memory_path, b"OUTSIDE_EDIT_PRESERVED", 0o600)
            return "FRESH"

        with self.assertRaises(Exception):
            self.prepare(edit)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.memory_path.read_bytes(), b"OUTSIDE_EDIT_PRESERVED")
        self.assertTrue(self.old_queue.exists())

    def test_interruption_after_move_resumes_exact_archive_without_deletion(self):
        import agent_brain_client.adapters.storage.reinstallation as storage

        move = storage.retain_item

        def interrupted(*args):
            move(*args)
            raise SystemExit("synthetic crash after retention move")

        with (
            patch.object(storage, "retain_item", side_effect=interrupted),
            self.assertRaises(SystemExit),
        ):
            self.prepare()
        self.assertEqual(read_journal(self.home)["phase"], "archiving")
        result = self.prepare()
        self.assertTrue(
            (
                Path(result["archive"])
                / "first-install/content-cache/outbox/retained_original_request.json"
            ).exists()
        )
        self.port.restore_mapping(self.cache, self.content)
        self.assertEqual(self.memory_path.read_text(), MEMORY["body"])

    def test_live_runtime_refuses_detachment_and_external_config_edit_is_preserved(
        self,
    ):
        before = self.config.read_bytes()
        with locked(self.cache / ".content-runtime.lock", timeout=0, cache=True):
            with self.assertRaises(Exception):
                self.prepare()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertTrue(self.old_queue.exists())
        atomic(self.config, before + b"# OUTSIDE EDIT\n", 0o600)
        with self.assertRaises(Exception):
            self.prepare()
        self.assertIn(b"# OUTSIDE EDIT", self.config.read_bytes())
        self.assertTrue(self.old_queue.exists())

    def test_different_server_account_and_content_scope_are_refused(self):
        self.prepare()
        connection = {
            "endpoint": "https://synthetic.invalid",
            "grant": copy.deepcopy(self.grant),
        }
        self.port.validate_connection(connection)
        for field, value in (
            ("actor", "other-owner"),
            ("ids", {k: "other-owner" for k in ("memory", "rule", "skill")}),
        ):
            changed = copy.deepcopy(connection)
            changed["grant"][field] = value
            with self.assertRaisesRegex(ValueError, "SAME Server/account"):
                self.port.validate_connection(changed)
        connection["endpoint"] = "https://other.invalid"
        with self.assertRaises(ValueError):
            self.port.validate_connection(connection)
        with self.assertRaises(ValueError):
            self.port.restore_mapping(self.cache, dict(self.content, kinds=["memory"]))
        self.assertFalse(self.memory_path.exists())

    def descriptor(self):
        if native_platform().startswith("win32-"):
            return {"kind": "windows-credential-manager", "source_sha256": "a" * 64}
        return {
            "kind": "mac-login-keychain",
            "source_sha256": "a" * 64,
            "helper": str(self.home / "unexecuted-helper"),
            "helper_sha256": "b" * 64,
        }

    def test_fresh_profile_is_distinct_without_modifying_original_pin(self):
        descriptor = self.descriptor()
        old = profile_metadata(
            "https://synthetic.invalid",
            self.grant,
            descriptor,
            str(self.root / "https"),
        )
        fresh = profile_metadata(
            "https://synthetic.invalid",
            self.grant,
            descriptor,
            str(self.root / "https"),
            instance_id="1" * 32,
        )
        self.assertNotEqual(
            io.digest(contract.encode(old)), io.digest(contract.encode(fresh))
        )
        self.assertEqual({k: v for k, v in fresh.items() if k != "instance_id"}, old)
        self.assertEqual(descriptor, self.descriptor())

    def test_public_reinstall_entry_reads_existing_memory_with_new_profile(self):
        import agent_brain_client.interfaces.cli.install as cli

        reads = []
        test = self

        class NewDevice:
            def __init__(self, path, digest):
                value = io.private_json(Path(path), digest)
                test.assertEqual(value["grant"]["actor"], "synthetic-owner")
                test.assertIn("instance_id", value)

            def request(self, request):
                reads.append(request)
                return {
                    "request_id": request["request_id"],
                    "result": contract.envelope("memory", "personal-owner", MEMORY),
                }

        def connect(home, native, endpoint, *, harness):
            self.port.validate_connection({"endpoint": endpoint, "grant": self.grant})
            fresh = read_journal(home)
            # The real material preparer creates private first-install metadata
            # before the cache. Windows cache directories retain inherited ACLs
            # and must not become a substitute private metadata parent.
            private_directory(self.profile.parent)
            self.port.restore_mapping(self.cache, self.content)
            profile = profile_metadata(
                endpoint,
                self.grant,
                self.descriptor(),
                str(self.root / "https"),
                instance_id=fresh["id"],
            )
            private_directory(self.profile.parent)
            io.write(self.profile, profile)
            io.write(self.root / "first-install/content-policy.json", self.content)
            policy = copy.deepcopy(self.policy)
            policy["runtime"]["https_profile_sha256"] = io.digest(
                self.profile.read_bytes()
            )
            io.write(self.root / "first-install/release-policy.json", policy)
            io.write(
                self.root / "codex-mcp.json",
                dict(
                    self.metadata,
                    release_policy_sha256=io.digest(
                        (self.root / "first-install/release-policy.json").read_bytes()
                    ),
                ),
            )
            return {"status": "content-native-client-installed", "installed": True}

        output = streamio.StringIO()
        with (
            patch.dict(os.environ, {"CODEX_HOME": str(self.native)}),
            patch.object(cli.Path, "home", return_value=self.home),
            patch.object(
                sys,
                "argv",
                [
                    "install.py", "--harness", "codex",
                    "--action",
                    "reinstall",
                    "--server",
                    "https://synthetic.invalid",
                ],
            ),
            patch("builtins.input", return_value="FRESH"),
            patch(
                "agent_brain_client.interfaces.cli.access.run_address_install",
                side_effect=connect,
            ),
            patch(
                "agent_brain_client.runtime.installation.finish_install",
                side_effect=lambda home, value, **kwargs: value,
            ),
            patch(
                "agent_brain_client.adapters.server.https.HTTPSContentClient", NewDevice
            ),
            patch.object(sys, "stdout", output),
        ):
            self.assertEqual(cli.main(), 0, output.getvalue())
        value = json.loads(output.getvalue())
        self.assertEqual(value["memory_accessibility"]["verified_records"], 1)
        self.assertFalse(value["old_identity_reused"])
        self.assertFalse(value["old_queue_replayed"])
        self.assertEqual([r["method"] for r in reads], ["content_read"])
        self.assertEqual(read_journal(self.home)["phase"], "complete")
        self.assertTrue(
            (
                Path(value["retained_archive"])
                / "first-install/content-cache/outbox/retained_original_request.json"
            ).exists()
        )
        self.assertEqual(
            self.native_memory.read_bytes(), b"SYNTHETIC_NATIVE_MEMORY_NOT_UPLOADED"
        )

    def test_codebuddy_global_and_project_entries_detach_only_receipt_owned_hooks(self):
        from agent_brain_client.adapters.harnesses.codebuddy import (
            configuration as buddy,
        )
        from agent_brain_client.adapters.harnesses.codex import (
            collection_installation as global_hooks,
        )

        native = self.home / ".codebuddy"
        private_directory(native)
        mcp, hooks = buddy.definitions(self.home / "unexecuted-buddy.py", self.home)
        paths = buddy.mcp_paths(self.home, "both")
        for path in paths:
            atomic(
                path,
                json.dumps(
                    {"mcpServers": {buddy.NAME: mcp, "other": {"command": "keep"}}}
                ).encode(),
                0o600,
            )
        other_group = {"hooks": [{"type": "command", "command": "synthetic-other"}]}
        atomic(
            native / "settings.json",
            json.dumps(
                {
                    "hooks": {
                        event: [other_group, group] for event, group in hooks.items()
                    }
                }
            ).encode(),
            0o600,
        )
        atomic(
            native / "CODEBUDDY.md",
            buddy.guidance("User notes\n", previous=False),
            0o600,
        )
        private_directory(self.root / "codebuddy")
        io.write(
            self.root / "codebuddy/registration.json",
            {
                "schema": 1,
                "surface": "both",
                "mcp_paths": list(map(str, paths)),
                "mcp": mcp,
                "hooks": hooks,
                "removed": False,
            },
        )
        io.write(self.root / "codebuddy/transaction.json", {"phase": "complete"})
        block = global_hooks.block({"device_home": str(self.home), "code_sha256": {}})
        atomic(self.config, self.config.read_bytes() + block.encode(), 0o600)
        private_directory(self.root / "global-memory")
        io.write(
            self.root / "global-memory/hook-install.json",
            {"phase": "complete", "block": block},
        )
        project = self.home / "project"
        private_directory(project / ".codex")
        target = project / ".codex/hooks.json"
        definitions = {
            "Stop": {
                "hooks": [
                    {
                        "statusMessage": "Agent Brain project memory",
                        "command": "synthetic-old-hook",
                    }
                ]
            }
        }
        atomic(
            target,
            json.dumps(
                {"hooks": {"Stop": [other_group, definitions["Stop"]]}}
            ).encode(),
            0o600,
        )
        settings = self.root / "project-memory/project/settings.json"
        private_directory(settings.parent)
        io.write(
            settings,
            {
                "schema": 1,
                "enabled": True,
                "source_mode": "codex-project",
                "device_home": str(self.home),
                "project_root": str(project),
                "off_file": str(settings.parent / "memory.off"),
            },
        )
        io.write(
            settings.parent / "hooks-install-synthetic.json",
            {
                "phase": "complete",
                "settings_sha256": io.digest(settings.read_bytes()),
                "target": str(target),
                "definitions": definitions,
            },
        )
        result = self.prepare()
        for path in paths:
            values = json.loads(path.read_bytes())["mcpServers"]
            self.assertEqual(values, {"other": {"command": "keep"}})
        self.assertNotIn(global_hooks.BEGIN, self.config.read_text())
        self.assertIn("User notes", (native / "CODEBUDDY.md").read_text())
        self.assertEqual(
            json.loads(target.read_bytes())["hooks"]["Stop"], [other_group]
        )
        for groups in json.loads((native / "settings.json").read_bytes())[
            "hooks"
        ].values():
            self.assertEqual(groups, [other_group])
        self.assertTrue(
            (Path(result["archive"]) / "codebuddy/registration.json").exists()
        )

    def test_native_write_interruption_rolls_back_own_image_then_resumes(self):
        from agent_brain_client.adapters.storage import reinstallation as storage

        apply = storage.host_apply

        def interrupted(rows, persist):
            apply(rows, persist)
            raise SystemExit("synthetic crash after native writes")

        with (
            patch.object(storage, "host_apply", side_effect=interrupted),
            self.assertRaises(SystemExit),
        ):
            self.prepare()
        self.assertEqual(read_journal(self.home)["phase"], "detaching")
        self.assertNotIn("agent_brain_server", self.config.read_text())
        result = self.prepare()
        self.assertEqual(result["status"], "fresh-install-ready")
        self.assertIn("[mcp_servers.other]", self.config.read_text())
        self.assertTrue(
            (
                Path(result["archive"])
                / "first-install/content-cache/outbox/retained_original_request.json"
            ).exists()
        )

    def test_wrong_server_is_rejected_before_confirmation_and_any_mutation(self):
        before = self.config.read_bytes()
        with self.port.lease(), self.assertRaisesRegex(ValueError, "SAME Server"):
            prepare_fresh_installation(
                self.port,
                lambda _: self.fail("Must reject before confirmation"),
                server="https://other.invalid",
            )
        self.assertIsNone(read_journal(self.home))
        self.assertEqual(before, self.config.read_bytes())
        self.assertTrue(self.old_queue.exists())

    def test_completed_memory_check_does_not_replay_or_reverify_deleted_records(self):
        from agent_brain_client.application.installation.fresh import (
            finish_fresh_installation,
        )

        self.prepare()

        class MemoryClient:
            def request(self, request):
                return {
                    "request_id": request["request_id"],
                    "result": contract.envelope("memory", "personal-owner", MEMORY),
                }

        result = finish_fresh_installation(
            self.port, MemoryClient(), "b" * 64, ["personal-owner"]
        )

        class NoRequest:
            def request(self, request):
                raise AssertionError(
                    "No new probe or queue replay on completed reconnect"
                )

        self.assertEqual(
            finish_fresh_installation(
                self.port, NoRequest(), "b" * 64, ["personal-owner"]
            ),
            result,
        )
        with self.assertRaises(ValueError):
            finish_fresh_installation(
                self.port, NoRequest(), "c" * 64, ["personal-owner"]
            )

    def test_codebuddy_shared_native_home_and_bootstrap_resume_are_preserved(self):
        from agent_brain_client.interfaces.cli.codebuddy import backend_home

        shared = self.root / "shared-native"
        self.native.rename(shared)
        self.native = shared
        self.config = shared / "config.toml"
        self.policy["runtime"]["codex_home"] = str(shared)
        io.write(self.root / "first-install/release-policy.json", self.policy)
        self.metadata["release_policy_sha256"] = io.digest(
            (self.root / "first-install/release-policy.json").read_bytes()
        )
        io.write(self.root / "codex-mcp.json", self.metadata)
        self.owned["binding"]["codex_home"] = str(shared)
        old_guidance = shared / "AGENTS.md"
        atomic(old_guidance, b"Retained old backend guidance.\n", 0o600)
        self.owned["files"][".codex/AGENTS.md"] = io.digest(old_guidance.read_bytes())
        self.owned["policy_sha"] = io.digest(
            contract.encode({"policy": self.content, "binding": self.owned["binding"]})
        )
        io.write(self.cache / "owned.json", self.owned)
        self.port = retained_installation(self.home, shared)
        self.prepare()
        self.assertEqual(backend_home(self.home), shared)
        self.assertTrue((shared / "memories/existing.md").exists())
        self.assertNotIn("agent_brain_server", self.config.read_text())
        self.port.restore_mapping(self.cache, self.content)
        self.assertEqual(self.memory_path.read_text(), MEMORY["body"])
        from agent_brain_client.runtime.harness_content import content_host
        actual = self.home / "selected-native-profile"
        private_directory(actual)
        agents = actual / "AGENTS.md"
        atomic(agents, b"Preserve the native user's instructions.\n", 0o600)
        destination = self.root / "codex/content-cache"
        with patch.dict(os.environ, CODEX_HOME=str(actual)):
            selected = retained_installation(self.home, shared, target_host=content_host("codex"))
            selected.restore_mapping(destination, self.content)
        restored = io.private_json(destination / "owned.json", cache=True)
        self.assertEqual(restored["binding"], {"device_home": str(self.home), "codex_home": str(actual)})
        self.assertNotIn(".codex/AGENTS.md", restored["files"])
        self.assertEqual(agents.read_bytes(), b"Preserve the native user's instructions.\n")
        self.assertEqual(old_guidance.read_bytes(), b"Retained old backend guidance.\n")
        self.assertEqual(self.memory_path.read_text(), MEMORY["body"])
        self.assertFalse((destination / "outbox").exists())

    def test_public_entry_advertises_explicit_reinstall_and_bootstrap_refresh(self):
        import subprocess
        from bootstrap.source_checkout import should_bootstrap

        self.assertTrue(should_bootstrap(["--action", "reinstall"]))
        entry = (
            Path(__file__).resolve().parents[1]
            / "src/agent_brain_client/interfaces/cli/install.py"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(entry), "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reinstall", result.stdout)

    def test_new_access_namespace_does_not_read_or_replace_old_system_items(self):
        import hashlib
        import agent_brain_client.interfaces.cli.access as access
        from agent_brain_client.adapters.platforms.macos import keychain
        from agent_brain_client.adapters.platforms.windows import native as windows

        services = []

        class Backend:
            def __init__(self, *args):
                services.append(args[-1])

            def load(self):
                raise AssertionError("No old identity or key read during preparation")

            def clear(self, value):
                raise AssertionError("No old system item removal")

        def build(helper, **kwargs):
            atomic(helper, b"SYNTHETIC_UNEXECUTED_HELPER", 0o600)
            source = (
                access.wizard.SOURCE
                / "src/agent_brain_client/adapters/platforms/macos/native/AccessCredentialStore.swift"
            )
            return {
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
            }

        backend_patch = (
            patch.object(windows, "WindowsCredentials", Backend)
            if native_platform().startswith("win32-")
            else patch.object(keychain, "NativeKeychain", Backend)
        )
        with backend_patch:
            access.access_store(
                self.home, "https://synthetic.invalid", {}, helper_builder=build
            )
            self.prepare()
            access.access_store(
                self.home, "https://synthetic.invalid", {}, helper_builder=build
            )
            access.access_store(
                self.home, "https://synthetic.invalid", {}, helper_builder=build
            )
        self.assertNotEqual(services[0], services[1])
        self.assertEqual(services[1], services[2])

    def test_wrong_authenticated_account_does_not_enroll_and_can_reprompt(self):
        import agent_brain_client.interfaces.cli.access as access
        from types import SimpleNamespace
        from unittest.mock import Mock

        self.prepare()
        backend = {"record": None}

        def save(value, expected):
            self.assertEqual(backend["record"], expected)
            backend["record"] = value

        def clear(expected):
            self.assertEqual(backend["record"], expected)
            backend["record"] = None

        store = SimpleNamespace(
            lease=nullcontext, load=lambda: backend["record"], save=save, clear=clear
        )
        connection = {
            "schema": 1,
            "endpoint": "https://synthetic.invalid",
            "grant": dict(self.grant, actor="different-owner"),
            "release": {
                "origin": "https://github.com/goodname427/agent-brain-client.git",
                "ref": "refs/heads/releases/personal-https-windows-stable"
                if native_platform().startswith("win32-")
                else "refs/heads/releases/personal-https-stable",
                "anchor_commit": "a" * 40,
            },
        }
        install = Mock()
        result = access.run_address_install(
            self.home,
            self.native,
            connection["endpoint"],
            store_factory=lambda *args: store,
            dependency_preparer=lambda *args: {},
            transport=SimpleNamespace(bootstrap=lambda *args: connection),
            key_input=lambda *args: "SYNTHETIC_ACCESS_KEY_" + "x" * 40,
            install=install,
         harness="codex", )
        self.assertEqual(result["status"], "fresh-install-account-mismatch")
        install.assert_not_called()
        self.assertIsNone(backend["record"])
        self.assertEqual(read_journal(self.home)["phase"], "ready")

    def test_old_completed_uninstall_can_archive_and_reconnect_without_native_entry(
        self,
    ):
        from agent_brain_client.runtime.client_lifecycle import remove_table
        import base64

        journal = {
            "schema": 1,
            "phase": "complete",
            "device_home": str(self.home),
            "registration": self.metadata,
            "before": {
                str(self.memory_path): {"data": "retained-by-prior-uninstall"},
                str(self.config): {
                    "data": base64.b64encode(self.config.read_bytes()).decode()
                },
            },
        }
        io.write(self.root / "uninstall-receipt.json", journal)
        (self.root / "codex-mcp.json").unlink()
        self.memory_path.unlink()
        io.write(self.cache / "owned.json", dict(self.owned, files={}))
        atomic(self.config, remove_table(self.config.read_bytes()), 0o600)
        result = self.prepare()
        self.assertEqual(result["status"], "fresh-install-ready")
        self.assertEqual(read_journal(self.home)["memory_ids"], ["personal-owner"])
        self.assertTrue((Path(result["archive"]) / "uninstall-receipt.json").exists())
        self.assertEqual(
            self.native_memory.read_bytes(), b"SYNTHETIC_NATIVE_MEMORY_NOT_UPLOADED"
        )

    def test_real_material_preparation_uses_new_instance_and_retained_content_policy(
        self,
    ):
        import agent_brain_client.runtime.installation as wizard

        content = copy.deepcopy(self.content)
        content["dependencies"]["commands"] = ["synthetic-reviewed-command"]
        self.content = content
        io.write(self.root / "first-install/content-policy.json", content)
        self.policy["runtime"]["content_policy_sha256"] = io.digest(
            (self.root / "first-install/content-policy.json").read_bytes()
        )
        io.write(self.root / "first-install/release-policy.json", self.policy)
        self.metadata["release_policy_sha256"] = io.digest(
            (self.root / "first-install/release-policy.json").read_bytes()
        )
        io.write(self.root / "codex-mcp.json", self.metadata)
        self.owned["policy_sha"] = io.digest(
            contract.encode({"policy": content, "binding": self.binding})
        )
        io.write(self.cache / "owned.json", self.owned)
        self.prepare()
        helper = self.home / "unexecuted-helper"
        atomic(helper, b"SYNTHETIC_HELPER", 0o600)
        source = Path(wizard.SOURCE) / (
            "src/agent_brain_client/adapters/platforms/windows/native.py"
            if native_platform().startswith("win32-")
            else "src/agent_brain_client/adapters/platforms/macos/native/CredentialStore.swift"
        )
        materials = {
            "source_sha256": io.digest(source.read_bytes()),
            "credential_helper": str(helper),
            "helper_sha256": io.digest(helper.read_bytes()),
            "dependencies": {
                "schema": 2,
                "python": str(helper),
                "git": str(helper),
                "git_exec_path": str(self.home),
                "python_sha256": io.digest(helper.read_bytes()),
                "git_sha256": io.digest(helper.read_bytes()),
            },
        }
        connection = {
            "schema": 1,
            "endpoint": "https://synthetic.invalid",
            "grant": self.grant,
            "release": {
                "origin": wizard.OFFICIAL_ORIGIN,
                "ref": "refs/heads/releases/personal-https-windows-stable"
                if native_platform().startswith("win32-")
                else wizard.OFFICIAL_REF,
                "anchor_commit": "a" * 40,
            },
        }

        class StopBeforeNativeOrNetwork(Exception):
            pass

        with (
            patch.object(wizard, "dependency_environment", return_value={}),
            self.assertRaises(StopBeforeNativeOrNetwork),
        ):
            wizard.prepare_materials(
                materials,
                self.home,
                self.native,
                connection,
                io.digest(contract.encode(connection)),
                runner=lambda *a, **k: (_ for _ in ()).throw(
                    StopBeforeNativeOrNetwork()
                ),
             harness="codex", )
        profile = io.private_json(self.profile)
        self.assertEqual(profile["instance_id"], read_journal(self.home)["id"])
        self.assertEqual(
            profile.get("credential_source_sha256")
            or profile["credential_backend"]["source_sha256"],
            io.digest(source.read_bytes()),
        )
        self.assertEqual(
            io.private_json(self.root / "first-install/content-policy.json"), content
        )
        self.assertEqual(self.memory_path.read_text(), MEMORY["body"])
        self.assertFalse((self.cache / "outbox").exists())

    def test_memory_unavailable_blocks_mapping_hooks_and_keeps_restored_files(self):
        import agent_brain_client.interfaces.cli.install as cli
        from unittest.mock import Mock

        def connect(*args, **kwargs):
            self.port.restore_mapping(self.cache, self.content)
            return {"status": "content-native-client-installed", "installed": True}

        output = streamio.StringIO()
        finish = Mock(
            side_effect=AssertionError(
                "Never refresh mapping/hooks before retained memory verification"
            )
        )
        with (
            patch.dict(os.environ, {"CODEX_HOME": str(self.native)}),
            patch.object(cli.Path, "home", return_value=self.home),
            patch.object(sys, "argv", ["install.py", "--harness", "codex", "--action", "reinstall"]),
            patch("builtins.input", return_value="FRESH"),
            patch(
                "agent_brain_client.interfaces.cli.access.run_address_install",
                side_effect=connect,
            ),
            patch("agent_brain_client.runtime.installation.finish_install", finish),
            patch(
                "agent_brain_client.runtime.fresh_installation.verify_memory",
                side_effect=ValueError("synthetic missing old memory"),
            ),
            patch.object(sys, "stdout", output),
        ):
            self.assertEqual(cli.main(), 2)
        value = json.loads(output.getvalue())
        finish.assert_not_called()
        self.assertEqual(
            value["memory_accessibility"]["status"], "review-or-resume-required"
        )
        self.assertEqual(self.memory_path.read_text(), MEMORY["body"])
        self.assertTrue(Path(value["retained_archive"]).is_dir())
        self.assertEqual(read_journal(self.home)["phase"], "ready")

    def test_private_profile_drift_after_confirmation_is_preserved_without_detaching(
        self,
    ):
        before = self.config.read_bytes()

        def edit_profile(_):
            io.write(self.profile, dict(self.old_profile, old_pin="b" * 64))
            return "FRESH"

        with self.assertRaisesRegex(ValueError, "metadata changed"):
            self.prepare(edit_profile)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(io.private_json(self.profile)["old_pin"], "b" * 64)
        self.assertTrue(self.old_queue.exists())

    def test_source_exclusions_and_isolation_survive_without_old_identity_or_approval(
        self,
    ):
        from agent_brain_client.adapters.storage.collection import (
            settings_path,
            seed_retained_routing,
        )
        from agent_brain_client.adapters.packages.layout import PYTHON_FILES

        old_scope = {
            "schema": 1,
            "profile_id": "1" * 32,
            "include_roots": [
                {"root": str(self.home / "isolated"), "sharing": "project"}
            ],
            "exclude_roots": [str(self.home / "private")],
            "non_project_roots": [str(self.home / "notes")],
            "recall_personal": False,
            "all_projects": True,
        }
        private_directory(settings_path(self.home).parent)
        io.write(
            settings_path(self.home),
            {
                "schema": 1,
                "device_home": str(self.home),
                "scope": old_scope,
                "old_code_pin": "preserved-but-not-executed",
                "enabled": True,
            },
        )
        result = self.prepare()
        record = read_journal(self.home)
        self.assertNotIn("profile_id", record["routing"])
        new_scope = dict(
            old_scope,
            profile_id="2" * 32,
            include_roots=[],
            exclude_roots=[],
            non_project_roots=[],
            recall_personal=True,
        )
        new_settings = {
            "schema": 1,
            "enabled": False,
            "device_home": str(self.home),
            "scope": new_scope,
            "approved_scope_sha256": None,
            "server_scope_sha256": None,
            "legacy_projects": [],
            "code_sha256": {name: "a" * 64 for name in PYTHON_FILES},
            "operator": None,
            "operator_sha256": None,
        }
        private_directory(settings_path(self.home).parent)
        io.write(settings_path(self.home), new_settings)
        # Model the new, privately prepared registration that exists before
        # the source setup stage; do not reuse the archived registration.
        private_directory(self.profile.parent)
        fresh_profile = profile_metadata(
            "https://synthetic.invalid",
            self.grant,
            self.descriptor(),
            str(self.root / "https"),
            instance_id=record["id"],
        )
        io.write(self.profile, fresh_profile)
        io.write(self.root / "first-install/content-policy.json", self.content)
        policy = copy.deepcopy(self.policy)
        policy["runtime"]["https_profile_sha256"] = io.digest(self.profile.read_bytes())
        io.write(self.root / "first-install/release-policy.json", policy)
        io.write(
            self.root / "codex-mcp.json",
            dict(
                self.metadata,
                release_policy_sha256=io.digest(
                    (self.root / "first-install/release-policy.json").read_bytes()
                ),
            ),
        )
        from agent_brain_client.application.installation.fresh import (
            finish_fresh_installation,
        )
        from agent_brain_client.runtime.fresh_installation import restore_source_routing

        class MemoryReader:
            def request(self, request):
                return {
                    "request_id": request["request_id"],
                    "result": contract.envelope("memory", "personal-owner", MEMORY),
                }

        finish_fresh_installation(
            self.port,
            MemoryReader(),
            io.digest(self.profile.read_bytes()),
            ["personal-owner"],
        )
        restore_source_routing(self.home)
        self.assertTrue(read_journal(self.home)["routing_restored"])
        restored = io.private_json(settings_path(self.home))
        self.assertEqual(restored["scope"]["profile_id"], "2" * 32)
        self.assertEqual(restored["scope"]["exclude_roots"], old_scope["exclude_roots"])
        self.assertEqual(restored["scope"]["include_roots"], old_scope["include_roots"])
        self.assertFalse(restored["scope"]["recall_personal"])
        self.assertIsNone(restored["approved_scope_sha256"])
        self.assertIsNone(restored["server_scope_sha256"])
        archived = Path(result["archive"]) / "global-memory/settings.json"
        self.assertEqual(io.private_json(archived)["scope"]["profile_id"], "1" * 32)
        current = copy.deepcopy(restored)
        current.update(approved_scope_sha256="b" * 64, server_scope_sha256="b" * 64)
        current["scope"]["exclude_roots"].append(str(self.home / "new-private"))
        io.write(settings_path(self.home), current)
        seed_retained_routing(self.home, record["routing"])
        self.assertEqual(io.private_json(settings_path(self.home)), current)

    def test_codebuddy_bootstrap_recovers_when_policy_moved_before_registration(self):
        from agent_brain_client.interfaces.cli.codebuddy import backend_home
        from agent_brain_client.adapters.storage import reinstallation as storage

        plan = self.port.plan

        def first_policy():
            value = plan()
            children = value["children"]
            value["children"] = {
                "first-install": children["first-install"],
                **{
                    name: identity
                    for name, identity in children.items()
                    if name != "first-install"
                },
            }
            return value

        move = storage.retain_item

        def interrupted(source, target, identity):
            move(source, target, identity)
            if source.name == "first-install":
                raise SystemExit("synthetic interruption before registration retention")

        with (
            patch.object(self.port, "plan", side_effect=first_policy),
            patch.object(storage, "retain_item", side_effect=interrupted),
            self.assertRaises(SystemExit),
        ):
            self.prepare()
        self.assertTrue((self.root / "codex-mcp.json").exists())
        self.assertFalse((self.root / "first-install/release-policy.json").exists())
        self.assertEqual(backend_home(self.home), self.native)
        self.assertEqual(self.prepare()["status"], "fresh-install-ready")

    def test_memory_probe_rejects_missing_record_and_catalog_protocol_error(self):
        class Client:
            def request(self, request):
                return {"request_id": request["request_id"], "result": {}}

        for identities in ([], ["personal-owner"]):
            with self.assertRaises(Exception):
                verify_memory_access(Client(), identities)


if __name__ == "__main__":
    unittest.main()
