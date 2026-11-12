"""Real registered launcher/worker restarts with synthetic native transport only."""


import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_content_install as installation_fixture
from test_content_outbox import Cloud, MEMORY
from agent_brain_client.adapters.harnesses.codex.content_ports import CodexContentPorts
from agent_brain_client.adapters.installation import installation
from agent_brain_client.adapters.storage.content_session import ContentSession
from agent_brain_client.adapters.platforms.filesystem import private_directory
from agent_brain_client.adapters.storage import metadata
from agent_brain_client.adapters.storage.reinstallation import (
    journal_path,
    read_journal,
)
from agent_brain_client.application.installation.fresh import finish_fresh_installation


@unittest.skipUnless(sys.platform == "darwin", "Native macOS release fixture")
class FreshRestart(unittest.TestCase):
    def setUp(self):
        self.f = installation_fixture.ContentInstall(
            "test_prepare_read_only_then_apply_inspect_reuse_and_single_link_state"
        )
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        # The package, release verification, registration, launcher, child MCP,
        # JSON-RPC initialize, synchronization and native mapping are real.
        # Only the signed OS/Server transport request is synthetic. No native
        # credential API or network Server is used, and no guard is patched.
        self.calls = self.f.base / "synthetic-transport-calls.jsonl"

        def transport_only(manifest, directory):
            name = "src/agent_brain_client/adapters/platforms/macos/component.py"
            path = directory / name
            shim = f"""
def _restart_test_request(self, request):
    import json
    from pathlib import Path
    from agent_brain_client.domain import contract
    with Path({str(self.calls)!r}).open("a") as log:
        log.write(json.dumps(request) + "\\n")
    if request["method"] == "content_catalog":
        result = {{"content_api_version": 1, "commit": "f" * 40, "artifacts": []}}
    else:
        result = contract.envelope(request["params"]["kind"], request["params"]["id"], None)
    return {{"request_id": request["request_id"], "result": result}}
ComponentClient.request = _restart_test_request
"""
            path.write_text(path.read_text() + shim)
            manifest["files"][name] = metadata.digest(path.read_bytes())

        self.f.publish("0.8.0", mutate=transport_only)
        self.f.active = self.f.channel.update()["active"]
        self.f.package = Path(self.f.active["package"])
        plan = self.f.plan()
        self.assertTrue(
            installation.apply(plan, self.f.digest(plan), lambda _: None)["installed"]
        )
        self.mcp = plan["mcp"]
        cloud = Cloud()
        cloud.grant = copy.deepcopy(self.f.grant)
        cloud.records[("memory", "synthetic-topic")] = copy.deepcopy(MEMORY)
        adapter = ContentSession(
            self.f.home,
            self.f.policy["runtime"]["content_cache"],
            self.f.content_value,
            cloud,
            host=CodexContentPorts, native_home=self.f.native,
        )
        self.adapter = adapter
        self.assertEqual(adapter.refresh()["status"], "content-refreshed")
        self.memory = (
            self.f.home / ".config/agent-brain-client/content/memory/synthetic-topic.md"
        )
        self.before = self.memory.read_bytes()
        archive = self.f.home / ".config/agent-brain-retained" / ("a" * 32)
        private_directory(archive)
        metadata.write(archive / "old-queue.json", {"synthetic": "not-replayed"})
        self.archive = archive
        private_directory(journal_path(self.f.home).parent)
        metadata.write(
            journal_path(self.f.home),
            {
                "schema": 1,
                "id": "a" * 32,
                "home": str(self.f.home),
                "native": str(self.f.native),
                "phase": "ready",
                "archive": str(archive),
                "cache_relative": "first-install/content-cache",
                "old_profile_sha256": "b" * 64,
                "service_enabled": True,
            },
        )

    def restart(self, command=None, payload=None):
        return subprocess.run(
            command or [self.mcp["command"], *self.mcp["args"]],
            input=json.dumps(
                payload
                or {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "synthetic-restart", "version": "1"},
                    },
                }
            )
            + "\n",
            text=True,
            capture_output=True,
            timeout=30,
            env=dict(os.environ, HOME=str(self.f.home), CODEX_HOME=str(self.f.native)),
        )

    def test_failed_memory_verification_then_real_registered_launcher_restart(self):
        class Port:
            def load(port):
                return read_journal(self.f.home)

            def save(port, value):
                metadata.write(journal_path(self.f.home), value)

        with self.assertRaises(ValueError):
            finish_fresh_installation(Port(), Cloud(), "c" * 64, ["synthetic-topic"])
        self.assertEqual(read_journal(self.f.home)["phase"], "ready")
        self.assertTrue(installation.inspect(self.f.home)["installed"])
        result = self.restart()
        self.assertEqual(
            self.memory.read_bytes() if self.memory.exists() else None,
            self.before,
            result.stdout + result.stderr,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("fresh-install-memory-verification-required", result.stderr)
        self.assertFalse(self.calls.exists())
        self.assertTrue((self.archive / "old-queue.json").exists())

    def test_direct_mcp_mapping_cli_and_hook_cannot_bypass_shared_gate(self):
        runtime = self.f.policy["runtime"]
        command = [
            sys.executable,
            "-I",
            "-B",
            str(self.f.package / "src/agent_brain_client/interfaces/mcp/content.py"),
            "--harness",
            "codex",
            "--client-version",
            "0.8.0",
        ]
        for key, value in runtime.items():
            command.extend(
                ["--" + key.replace("_", "-").replace("content-cache", "cache"), value]
            )
        for suffix in ([], ["--mapping-refresh"], ["--event-hook"]):
            with self.subTest(mode=suffix):
                result = self.restart(command + suffix)
                self.assertEqual(
                    result.returncode,
                    0 if suffix == ["--event-hook"] else 2,
                    result.stdout + result.stderr,
                )
                self.assertIn(
                    "fresh-install-memory-verification-required", result.stderr
                )
                self.assertEqual(self.memory.read_bytes(), self.before)
        # A pre-existing in-process adapter must recheck the same durable gate.
        with self.assertRaisesRegex(
            ValueError, "fresh-install-memory-verification-required"
        ):
            self.adapter.refresh()
        self.assertFalse(self.calls.exists())

    def test_verified_memory_unlocks_real_registered_worker_and_only_that_profile(self):
        class Port:
            def load(port):
                return read_journal(self.f.home)

            def save(port, value):
                metadata.write(journal_path(self.f.home), value)

        cloud = Cloud()
        cloud.records[("memory", "synthetic-topic")] = copy.deepcopy(MEMORY)
        proof = finish_fresh_installation(Port(), cloud, "c" * 64, ["synthetic-topic"])
        self.assertEqual(proof["verified_records"], 1)
        from agent_brain_client.adapters.storage.reinstallation import (
            require_memory_verified,
        )

        with self.assertRaisesRegex(
            ValueError, "fresh-install-memory-verification-required"
        ):
            require_memory_verified(self.f.home, "d" * 64)
        result = self.restart()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reply = json.loads(result.stdout)
        self.assertEqual(
            reply["result"]["content_mapping"]["status"], "content-refreshed"
        )
        self.assertTrue(self.calls.exists())
        self.assertTrue((self.archive / "old-queue.json").exists())

    def test_fresh_install_plan_preserves_disabled_mcp(self):
        from agent_brain_client.runtime.client_lifecycle import remove_table

        record = read_journal(self.f.home)
        record["service_enabled"] = False
        metadata.write(journal_path(self.f.home), record)
        self.f.config.write_bytes(remove_table(self.f.config.read_bytes()))
        (self.f.home / ".config/agent-brain-client/codex-mcp.json").unlink()
        plan = self.f.plan()
        self.assertFalse(plan["mcp"]["enabled"])


if __name__ == "__main__":
    unittest.main()
