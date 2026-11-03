"""Real isolated launcher processes and MCP handshakes; fictional content only."""

import io
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from brain_lock import native_platform, private_directory, private_mode
from brain_release import ReleaseEngine, UpdateBlocked, atomic_json, digest
from client_release import CAPABILITIES, ClientReleases
from content_mcp import ContentServer, serve
from test_content_error_stages import Adapter

ROOT = Path(__file__).resolve().parents[1]


class StartupConcurrency(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="synthetic-startup-")
        self.addCleanup(temp.cleanup)
        # ReleaseEngine also locks local.parent/python-write.lock. Protect the
        # freshly created fixture root before creating any cache, lock or body.
        self.base = Path(temp.name).resolve() / "fixture"
        private_directory(self.base)
        private_mode(self.base, directory=True, owner=True)
        self.processes = []
        self.addCleanup(self.close)
        for name in ("home", "codex"):
            (self.base / name).mkdir()
        self.policy = {
            "channel": "synthetic",
            "core_api": 1,
            "state_schema": 1,
            "capabilities": CAPABILITIES,
            "file_prefixes": ["adapters/"],
            "platform": native_platform(),
            "runtime": {
                "endpoint": "https://synthetic.invalid",
                "token_file": str(self.base / "unused-token"),
            },
        }
        worker = """import sys
from pathlib import Path
sys.path[:0]=[str(Path(ROOT)/p) for p in ('scripts','adapters/codex','tests')]
from content_mcp import ContentServer,serve
from test_content_error_stages import Adapter
adapter=Adapter();adapter.refresh=lambda:{'status':'synthetic','model_loaded':'unknown'}
version=sys.argv[sys.argv.index('--client-version')+1]
serve(ContentServer(adapter,version),sys.stdin.buffer,sys.stdout)
""".replace("ROOT", repr(str(ROOT)))
        self.payload = {"adapters/codex/remote_mcp.py": worker.encode()}
        self.manifest = {
            "schema": 1,
            "version": "0.6.1",
            "channel": "synthetic",
            "core_api": 1,
            "state_schema": 1,
            "python_min": "3.11",
            "capabilities": CAPABILITIES,
            "migrations": [],
            "files": {k: digest(v) for k, v in self.payload.items()},
        }
        self.engine = ReleaseEngine(self.base / "cache", self.policy, None, Mock(), {})
        package = self.engine.prepare("a" * 40, self.manifest, self.payload.__getitem__)
        self.old = {
            "version": "0.6.1",
            "commit": "a" * 40,
            "package": str(package),
            "manifest_sha256": digest(
                json.dumps(self.manifest, sort_keys=True).encode()
            ),
        }
        self.engine.set_active(self.old)
        (self.base / "policy.json").write_text(json.dumps(self.policy))
        target = dict(self.manifest, version="0.6.2")
        (self.base / "target.json").write_text(json.dumps(target))
        (self.base / "payload.json").write_text(
            json.dumps({k: v.decode() for k, v in self.payload.items()})
        )
        driver = """import json,os,sys,time
from pathlib import Path
from unittest.mock import Mock
root=Path(ROOT)
sys.path[:0]=[str(root/p) for p in ('scripts','adapters/codex')]
from brain_release import ReleaseEngine,atomic_json
from client_release import ClientReleases
import client_startup
base=Path(sys.argv[1]);mode=sys.argv[2]
policy=json.loads((base/'policy.json').read_bytes())
class Source:
 def latest(self):
  count=base/'cache'/'checks.json'
  atomic_json(count,{'count':json.loads(count.read_bytes())['count']+1 if count.exists() else 1})
  (base/'entered').touch()
  deadline=time.monotonic()+15
  while not (base/'release').exists():
   if time.monotonic()>deadline:raise OSError('Synthetic offline deadline')
   time.sleep(.02)
  payload=json.loads((base/'payload.json').read_bytes())
  return 'b'*40,json.loads((base/'target.json').read_bytes()),lambda p:payload[p].encode()
class Releases(ClientReleases):
 def update(self,**options):
  result=super().update(**options)
  safe={key:result[key] for key in ('status','diagnostic') if key in result}
  (base/('update-'+str(os.getpid())+'.json')).write_text(json.dumps(safe))
  return result
release=object.__new__(Releases);release.policy=policy;release.inspection=False
adapter=Mock();adapter.snapshot.return_value={}
release.engine=ReleaseEngine(base/'cache',policy,Source(),adapter,{})
if mode=='onboard':
 print(json.dumps(release.update()),flush=True)
else:
 client_startup.ClientReleases=lambda *args:release
 result=client_startup.startup('synthetic-unused','a'*64)
 sys.exit(result.get('exit_code',2))
""".replace("ROOT", repr(str(ROOT)))
        self.driver = self.base / "driver.py"
        self.driver.write_text(driver)

    def close(self):
        (self.base / "release").touch()
        for process in self.processes:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()

    def spawn(self, mode="serve"):
        env = dict(
            os.environ,
            HOME=str(self.base / "home"),
            USERPROFILE=str(self.base / "home"),
            CODEX_HOME=str(self.base / "codex"),
        )
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(self.driver), str(self.base), mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.processes.append(process)
        return process

    def read(self, process):
        output = queue.Queue()
        threading.Thread(
            target=lambda: output.put(process.stdout.readline()), daemon=True
        ).start()
        try:
            line = output.get(timeout=5)
        except queue.Empty:
            self.fail("Synthetic MCP response missed its startup budget")
        self.assertTrue(line, "Synthetic worker closed before handshake")
        return json.loads(line)

    def send(self, process, value):
        process.stdin.write(json.dumps(value).encode() + b"\n")
        process.stdin.flush()

    def ready(self, process, version="0.6.1"):
        self.send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "synthetic-concurrency", "version": "1"},
                },
            },
        )
        response = self.read(process)
        self.assertEqual(response["result"]["serverInfo"]["version"], version)
        self.send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in self.read(process)["result"]["tools"]}
        self.assertIn("content_catalog", names)
        self.assertFalse((self.base / "unused-token").exists())

    def wait_for(self, name):
        deadline = time.monotonic() + 5
        while not (self.base / name).exists():
            if time.monotonic() > deadline:
                diagnostics = [
                    json.loads(path.read_bytes())
                    for path in self.base.glob("update-*.json")
                ]
                self.fail(
                    "Synthetic update barrier did not arrive: "
                    + json.dumps(diagnostics)
                )
            time.sleep(0.02)

    def test_single_session_handshakes_while_its_background_check_is_blocked(self):
        process = self.spawn()
        self.ready(process)
        self.wait_for("entered")
        second = self.spawn()
        self.ready(second)
        self.assertEqual(
            json.loads((self.engine.local / "checks.json").read_bytes())["count"], 1
        )
        self.assertEqual(self.engine.serving_active(), self.old)

    def test_onboarding_and_two_sessions_do_not_queue_handshakes_behind_update(self):
        onboarding = self.spawn("onboard")
        self.wait_for("entered")
        first, second = self.spawn(), self.spawn()
        self.ready(first)
        self.ready(second)
        self.assertEqual(
            json.loads((self.engine.local / "checks.json").read_bytes())["count"], 1
        )
        (self.base / "release").touch()
        self.assertEqual(self.read(onboarding)["status"], "updated")
        self.assertEqual(self.engine.serving_active()["version"], "0.6.2")
        self.send(
            first,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "content_catalog",
                    "arguments": {"kinds": ["memory"]},
                },
            },
        )
        runtime = self.read(first)["result"]["structuredContent"]["client_runtime"]
        self.assertEqual(runtime["running_worker_version"], "0.6.1")
        self.ready(self.spawn(), version="0.6.2")

    def test_unhealthy_legacy_candidate_is_never_selected_and_corruption_blocks(self):
        bad = dict(self.manifest, version="0.6.2")
        package = self.engine.prepare("b" * 40, bad, self.payload.__getitem__)
        candidate = {
            "version": "0.6.2",
            "commit": "b" * 40,
            "package": str(package),
            "manifest_sha256": digest(json.dumps(bad, sort_keys=True).encode()),
        }
        for phase in ("applying", "activated"):
            atomic_json(
                self.engine.journal,
                {"phase": phase, "before": self.old, "target": candidate},
            )
            self.engine.set_active(candidate)
            self.assertEqual(self.engine.serving_active(), self.old)
        Path(self.old["package"], "adapters/codex/remote_mcp.py").write_bytes(
            b"corrupted"
        )
        with self.assertRaises(UpdateBlocked):
            self.engine.serving_active()

    def test_first_install_pending_health_and_changing_snapshot_fail_closed(self):
        atomic_json(
            self.engine.journal,
            {"phase": "activated", "before": None, "target": self.old},
        )
        self.assertIsNone(self.engine.serving_active())
        self.engine.journal.unlink()
        original = self.engine.active

        def interleave(value):
            result = original(value)
            atomic_json(self.engine.journal, {"phase": "applying", "before": self.old})
            return result

        with patch.object(self.engine, "active", side_effect=interleave):
            with self.assertRaises(UpdateBlocked):
                self.engine.serving_active()

    def test_installation_inspection_uses_snapshot_and_keeps_ownership_checks(self):
        import content_install as install
        from brain_lock import locked

        home, native = self.base / "home", self.base / "codex"
        state = home / ".config" / "agent-brain-client"
        private_directory(state)
        registration = state / "codex-mcp.json"
        config = native / "config.toml"
        mcp = {"command": "synthetic-worker", "enabled": True}
        config.write_bytes(install.render(b"", mcp, False))
        meta = {
            "content_candidate": True,
            "release_policy": "synthetic-policy",
            "release_policy_sha256": "a" * 64,
            "package_sha256": "b" * 64,
            "plan_sha256": "c" * 64,
            "mcp_sha256": install.registered_mcp_digest(mcp),
            "owned_install_paths": [str(config), str(registration)],
        }
        install.save(registration, meta)
        install.save(
            state / "content-install-receipt.json",
            {
                "phase": "complete",
                "plan_sha256": meta["plan_sha256"],
                "after": {
                    str(config): digest(config.read_bytes()),
                    str(registration): digest(registration.read_bytes()),
                },
            },
        )
        target = dict(self.manifest, version="0.6.2")
        package = self.engine.prepare("b" * 40, target, self.payload.__getitem__)
        candidate = {
            "commit": "b" * 40,
            "version": "0.6.2",
            "package": str(package),
            "manifest_sha256": digest(json.dumps(target, sort_keys=True).encode()),
        }
        (package / "adapters/codex/remote_mcp.py").write_bytes(b"damaged candidate")
        atomic_json(
            self.engine.journal,
            {"phase": "activated", "before": self.old, "target": candidate},
        )
        self.engine.set_active(candidate)
        release = object.__new__(ClientReleases)
        release.policy, release.engine = self.policy, self.engine
        # Only context derivation is supplied here. The full Mac fixture below
        # verifies a real installed registration/profile/bootstrap; this shared
        # test exercises native locks, receipt/MCP ownership and release hashes.
        context = (self.policy, mcp, meta["package_sha256"], native)
        with (
            locked(self.engine.local / "update.lock", timeout=0),
            patch.object(install, "registered_context", return_value=context),
            patch.object(install, "ClientReleases", return_value=release),
        ):
            result = install.inspect(home)
            self.assertTrue(result["installed"], result)
            self.assertEqual(result["installed_version"], "0.6.1")
            config.write_bytes(
                install.render(
                    config.read_bytes(), dict(mcp, command="synthetic-drift"), True
                )
            )
            self.assertFalse(install.inspect(home)["installed"])

    def test_failed_health_restores_previous_package_and_redacts_failure(self):
        target = dict(self.manifest, version="0.6.2")
        self.engine.source = Mock()
        self.engine.source.latest.return_value = (
            "b" * 40,
            target,
            self.payload.__getitem__,
        )
        self.engine.adapter.snapshot.return_value = {}
        self.engine.adapter.health.side_effect = UpdateBlocked(
            "SYNTHETIC_SECRET_HEALTH_DETAIL"
        )
        release = object.__new__(ClientReleases)
        release.engine = self.engine
        release.policy = self.policy
        release.inspection = False
        result = release.update()
        self.assertEqual(result["status"], "release-review-required")
        self.assertEqual(self.engine.serving_active(), self.old)
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))

    def test_background_burst_marker_is_bound_to_policy_and_does_not_claim_latest(self):
        release = object.__new__(ClientReleases)
        release.engine = self.engine
        release.policy = dict(self.policy)
        release.update = Mock(return_value={"status": "current"})
        release.startup_check()
        release.startup_check()
        release.update.assert_called_once_with(lock_timeout=0)
        release.policy["channel"] = "synthetic-other"
        release.startup_check()
        self.assertEqual(release.update.call_count, 2)
        marker = json.loads((self.engine.local / "startup-check.json").read_bytes())
        self.assertEqual(set(marker), {"policy_sha256", "checked_at"})


class EvidenceDiagnostics(unittest.TestCase):
    def test_formal_tool_metadata_and_current_run_evidence_guidance(self):
        adapter = Adapter()
        adapter.refresh = lambda: {"status": "synthetic"}
        server = ContentServer(adapter)
        result = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "synthetic", "version": "1"},
                },
            }
        )
        for text in (
            "synthetic sandbox",
            "older result",
            "event receipt only",
            "model_extraction_verified=false",
        ):
            self.assertIn(text, result["instructions"])
        self.assertEqual(server.client_runtime["service_mode"], "approved-content")

    def test_remote_failure_and_synthetic_tools_remain_distinct(self):
        from remote_mcp import Server

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "synthetic", "version": "1"},
            },
        }
        synthetic = Server(None).handle(request)
        self.assertIn("Synthetic personal data only", synthetic["instructions"])
        adapter = Adapter()
        adapter.invoke = lambda request: {
            "request_id": request["request_id"],
            "error": {
                "code": "synthetic_remote_unavailable",
                "stage": "remote",
                "retryable": True,
            },
            "persistence": {"durable": False},
        }
        server = ContentServer(adapter)
        server.state = "ready"
        result = server.invoke(
            "content_read", {"kind": "memory", "id": "synthetic-topic"}
        )
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["stage"], "remote")

    def test_pre_handshake_and_missing_tool_have_separate_safe_diagnostics(self):
        adapter = Adapter()
        server = ContentServer(adapter)
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "synthetic", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "synthetic_unavailable", "arguments": {}},
            },
        ]
        adapter.refresh = lambda: {"status": "synthetic"}
        output = io.StringIO()
        serve(
            server,
            io.BytesIO(b"".join(json.dumps(r).encode() + b"\n" for r in requests)),
            output,
        )
        errors = [
            json.loads(line)["error"]["data"]
            for line in output.getvalue().splitlines()
            if "error" in json.loads(line)
        ]
        self.assertEqual([e["stage"] for e in errors], ["handshake", "tools-ready"])


if __name__ == "__main__":
    unittest.main()
