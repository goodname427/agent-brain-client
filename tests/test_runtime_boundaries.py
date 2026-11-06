"""Shared storage, dependency fences and historical queue replay without a host."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import agent_brain_client.domain.contract as contract
from agent_brain_client.adapters.storage.content import FileEventQueue
from agent_brain_client.domain.errors import CacheBlocked
from agent_brain_client.adapters.storage.host_files import atomic
from agent_brain_client.adapters.platforms.filesystem import cache_directory
from dev.checks.source import check_layer_boundary
from test_content_outbox import POLICY
from test_event_hook import accepted

ROOT = Path(__file__).resolve().parents[1]


class RuntimeBoundaries(unittest.TestCase):
    def test_shared_imports_work_without_any_adapter_or_installer_on_path(self):
        code = """import sys
sys.path.insert(0,sys.argv[1])
import agent_brain_client.adapters.storage.content
import agent_brain_client.adapters.storage.host_files
import agent_brain_client.adapters.packages.client
import agent_brain_client.adapters.server.https
import agent_brain_client.adapters.server.ssh
import agent_brain_client.interfaces.mcp.topics
assert not {'content_cache','content_mcp','codex_connect','project_memory','first_install'} & set(sys.modules)
"""
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, str(ROOT / "src")],
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_dependency_fence_rejects_direct_qualified_and_dynamic_host_imports(self):
        for source in (
            "import agent_brain_client.adapters.harnesses.codex.content as cache",
            "from agent_brain_client.adapters.harnesses.codex import content",
            "from agent_brain_client.runtime.installation import run",
            "from dev.build import package",
            "__import__('agent_brain_client.adapters.harnesses.codex.configuration')",
            "importlib.import_module('agent_brain_client.adapters.harnesses.codex.project')",
        ):
            with (
                self.subTest(source=source),
                self.assertRaisesRegex(ValueError, "dependency boundary"),
            ):
                check_layer_boundary("src/agent_brain_client/domain/example.py", source)
        for module in ("agent_brain_client.adapters.harnesses.codex.content", "agent_brain_client.adapters.harnesses.codex.configuration", "agent_brain_client.adapters.harnesses.codex.project"):
            with self.assertRaises(ValueError):
                check_layer_boundary(
                    "src/agent_brain_client/adapters/harnesses/codebuddy/events.py", "import " + module
                )
        check_layer_boundary(
            "src/agent_brain_client/domain/example.py", "from agent_brain_client.domain.events import event"
        )

    def test_old_source_aliases_are_absent_and_current_frozen_files_exist(self):
        import agent_brain_client.adapters.harnesses.codex.collection_installation as global_memory

        for name in (
            "adapters/codex/content_contract.py",
            "adapters/codex/remote_http.py",
            "tools/memory_automation.py",
            "tools/global_memory.py",
            "tools/mac_first_install.py",
        ):
            self.assertFalse((ROOT / name).exists(), name)
        self.assertTrue(all((ROOT / name).is_file() for name in global_memory.FILES))
        self.assertEqual(set(global_memory.FILES), set(__import__('agent_brain_client.adapters.packages.layout', fromlist=['PYTHON_FILES']).PYTHON_FILES))
        # Existing frozen CodeBuddy loaders accept only these original roots.
        self.assertLessEqual({name.split('/')[0] for name in global_memory.FILES},
                             {'src'})

    def test_historical_pending_event_replays_exact_bytes_after_storage_split(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve() / "home"
            home.mkdir()
            cache = home / "queue"
            binding = {
                "device_home": str(home),
                "codex_home": str(home / "shared-native"),
            }
            identity = {
                "service": "synthetic",
                "actor": "synthetic",
                "host_adapter": "codebuddy",
            }
            policy_sha = hashlib.sha256(
                contract.encode({"policy": POLICY, "binding": binding})
            ).hexdigest()
            queue_sha = hashlib.sha256(
                contract.encode({"policy_sha": policy_sha, "service": identity})
            ).hexdigest()
            request = {
                "request_id": "codebuddy_event_" + "a" * 64,
                "method": "memory_event_submit",
                "params": {
                    "kind": "memory",
                    "id": "evt-codebuddy-" + "a" * 48,
                    "event": {
                        "contract": "agentbrain.event.v1",
                        "event_id": "evt-codebuddy-" + "a" * 48,
                        "project_id": "synthetic-project",
                        "session_id": "synthetic-session",
                        "occurred_at": "2026-11-05T00:00:00.000Z",
                        "messages": [
                            {"role": "user", "content": "Synthetic pending turn"}
                        ],
                    },
                },
            }
            # The existing schema-1 envelope and pre-split binding are the
            # upgrade contract. A closed gate must leave these bytes untouched.
            row = dict(
                schema=1,
                policy_sha=queue_sha,
                created=1,
                request=request,
                request_sha=hashlib.sha256(contract.encode(request)).hexdigest(),
                state="pending",
                response=None,
                remote=None,
                reconciles={"request_ids": [], "source_keys": []},
                resolution_request_id=None,
            )
            directory = cache / "outbox"
            cache_directory(cache)
            cache_directory(directory)
            file = directory / (request["request_id"] + ".json")
            raw = contract.encode(row)
            atomic(file, raw, 0o600, cache=True)
            calls = []

            class Client:
                grant = {
                    "scopes": ["read", "put"],
                    "artifact_types": POLICY["kinds"],
                    "ids": POLICY["ids"],
                }
                queue_identity = identity

                def request(self, value):
                    calls.append(copy.deepcopy(value))
                    return accepted(value)

            queue = FileEventQueue(home, cache, POLICY, Client(), binding=binding)
            self.assertEqual(queue.queue_sha, queue_sha)
            self.assertEqual(
                queue.invoke(request, event_gate=lambda: False)["local_queue"]["state"],
                "pending",
            )
            self.assertEqual(file.read_bytes(), raw)
            self.assertEqual(calls, [])
            restarted = FileEventQueue(home, cache, POLICY, Client(), binding=binding)
            self.assertEqual(
                restarted.invoke(request, event_gate=lambda: True)["local_queue"][
                    "state"
                ],
                "confirmed",
            )
            self.assertEqual(calls, [request])
            self.assertEqual(json.loads(file.read_bytes())["request"], request)
            changed = copy.deepcopy(request)
            changed["params"]["event"]["messages"][0]["content"] = "Changed text"
            with self.assertRaises(CacheBlocked):
                restarted.invoke(changed, event_gate=lambda: True)
            with self.assertRaises(CacheBlocked):
                restarted.sources._assert_sources_shareable(["a" * 64])
