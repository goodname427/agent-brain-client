"""Portable retained-reinstall orchestration; not Windows package acceptance."""

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import tomllib
import unittest
from unittest.mock import Mock, patch

import test_fresh_installation as fixture
from agent_brain_client.adapters.harnesses.codex import binding
from agent_brain_client.adapters.harnesses.codex import collection_installation as hooks
from agent_brain_client.adapters.installation import installation
from agent_brain_client.adapters.platforms.filesystem import private_directory, native_platform
from agent_brain_client.adapters.storage import metadata, reinstallation, collection
from agent_brain_client.interfaces.cli import access


def snapshot(directory):
    return {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


class RetainedReinstallBindings(unittest.TestCase):
    def test_interrupt_then_resume_same_archive_without_rewriting_native_registration(self):
        old = fixture.FreshInstallation()
        old.setUp()
        self.addCleanup(old.doCleanups)
        retain = reinstallation.retain_item

        def interrupt(*args):
            retain(*args)
            raise SystemExit("Synthetic interruption after retention move")

        with (
            patch.object(reinstallation, "retain_item", side_effect=interrupt),
            self.assertRaises(SystemExit),
        ):
            old.prepare()
        unfinished = reinstallation.read_journal(old.home)
        self.assertEqual(unfinished["phase"], "archiving")
        self.assertEqual(unfinished["native"], str(old.native))
        ready = old.prepare()
        journal = reinstallation.read_journal(old.home)
        self.assertEqual(journal["id"], unfinished["id"])
        self.assertEqual(journal["archive"], unfinished["archive"])
        archive = Path(ready["archive"])
        retained = snapshot(archive)
        self.assertEqual(
            retained[old.old_queue.relative_to(old.root)],
            b"SYNTHETIC_UNSENT_OLD_QUEUE_DO_NOT_REPLAY",
        )
        native = old.root / "shared-native"
        endpoint = journal["endpoint"]
        release = (
            "refs/heads/releases/personal-https-windows-stable"
            if native_platform().startswith("win32-")
            else "refs/heads/releases/personal-https-stable"
        )
        connection = {
            "schema": 1,
            "endpoint": endpoint,
            "grant": old.grant,
            "release": {
                "origin": "https://github.com/goodname427/agent-brain-client.git",
                "ref": release,
                "anchor_commit": "a" * 40,
            },
        }
        credential = {}
        store = SimpleNamespace(
            lease=nullcontext,
            load=lambda: credential or None,
            save=lambda value, expected: credential.update(value),
            clear=Mock(side_effect=AssertionError("No credential clear")),
        )
        peer = SimpleNamespace(
            bootstrap=Mock(return_value=connection),
            replay=Mock(side_effect=AssertionError("No old queue replay")),
        )
        key_input = Mock(return_value="SYNTHETIC_ACCESS_KEY_" + "x" * 40)
        command = {
            "command": str(Path(__file__).resolve()),
            "args": ["--harness", "codex"],
            "enabled": True,
        }
        policy_file = old.root / "synthetic-policy.json"
        registration = old.root / "codex-mcp.json"

        def install(home, target, authenticated, digest, **kwargs):
            self.assertEqual((home, target, authenticated), (old.home, native, connection))
            self.assertEqual(kwargs["harness"], "codex")
            # Substitute the trusted package/pairing boundary. The addressed
            # resume, old journal checks and both native TOML transactions are real.
            private_directory(target)
            policy = {"runtime": {"codex_home": str(target)}}
            metadata.write(policy_file, policy)
            metadata.write(
                registration,
                {
                    "release_policy": str(policy_file),
                    "release_policy_sha256": metadata.digest(policy_file.read_bytes()),
                },
            )
            with patch.object(installation, "registered_context", return_value=(policy, command, None, None)):
                mcp = binding.install(home, native=old.native)
            hook_state = collection.settings_path(home).parent
            private_directory(hook_state)
            hook = hooks.transact(
                old.native,
                hook_state,
                hooks.block({"device_home": str(home), "code_sha256": {}}),
            )
            return {"installed": True, "mcp": mcp, "hook": hook}

        def resume():
            result = access.run_address_install(
                old.home, native, endpoint, harness="codex", install=install,
                store_factory=lambda *args: store, transport=peer, key_input=key_input,
                dependency_preparer=lambda *_: {},
            )
            self.assertTrue(result["installed"], result)
            return result

        first = resume()
        self.assertTrue(first["mcp"]["native_changed"])
        self.assertTrue(first["hook"]["native_changed"])
        config = old.config.read_bytes()
        parsed = tomllib.loads(config.decode())
        self.assertEqual(set(parsed["mcp_servers"]), {"other", binding.NAME})
        self.assertEqual(set(parsed["hooks"]), {"SessionStart", "UserPromptSubmit", "Stop"})
        self.assertEqual(parsed["mcp_servers"][binding.NAME]["env"]["CODEX_HOME"], str(old.native))

        second = resume()
        self.assertFalse(second["mcp"]["native_changed"])
        self.assertFalse(second["hook"]["native_changed"])
        self.assertEqual(old.config.read_bytes(), config)
        self.assertEqual(snapshot(archive), retained)
        self.assertEqual(reinstallation.read_journal(old.home), journal)
        self.assertEqual(list(archive.parent.iterdir()), [archive])
        self.assertFalse(old.old_queue.exists())
        key_input.assert_called_once()
        peer.replay.assert_not_called()
