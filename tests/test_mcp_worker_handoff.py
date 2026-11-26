"""Isolated stdio subprocess regressions for the common HTTPS content handoff."""

import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_brain_client.adapters.packages.client import HTTPS_CONTENT_CAPABILITIES, SSH_CONTENT_CAPABILITIES
from agent_brain_client.adapters.storage.content import CONTENT_CACHE_ABI
from agent_brain_client.interfaces.mcp.content import ManagedContentServer
from agent_brain_client.interfaces.mcp.framing import write_response
from agent_brain_client.interfaces.mcp.server import (
    DRAIN_METHOD,
    HANDOFF_VERSION,
    REQUEST_DEADLINE_HEADER,
    WorkerSpec,
    serve,
    supervise_stdio,
)

PROTOCOL = "2025-11-25"
INIT = {
    "protocolVersion": PROTOCOL,
    "capabilities": {},
    "clientInfo": {"name": "local-handoff-fixture", "version": "1"},
}


def wait_until(predicate, *, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Local fixture did not reach the expected state")


def marker(root, name):
    (root / name).touch()


class FixtureServer:
    def __init__(self, root, label, protocol):
        self.root = root
        self.label = label
        self.ready = protocol == PROTOCOL
        self.catalog = [{
            "name": "fixture",
            "description": "Worker " + label,
            "inputSchema": {"type": "object" if label != "schema" else "array"},
        }]

    def handle(self, request):
        method = request["method"]
        if method == "initialize":
            marker(self.root, "initialized-" + self.label)
            return {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}}}
        if method == "notifications/initialized":
            self.ready = True
            return None
        if method == DRAIN_METHOD:
            return {}
        if not self.ready:
            raise AssertionError("Tools require the existing handshake")
        if method == "tools/list":
            return {"tools": self.catalog}
        action = request["params"]["arguments"]["action"]
        if action in ("hold", "hold-new"):
            marker(self.root, "holding-" + self.label)
            release = "release-new" if action == "hold-new" else "release"
            wait_until(lambda: (self.root / release).exists(), timeout=12)
        if action == "crash":
            with (self.root / "invocations").open("ab") as log:
                log.write(b"crash\n")
            os._exit(9)
        if action == "queued":
            marker(self.root, "queued-" + self.label)
        return {"worker": self.label}


class FixtureInput:
    def __init__(self, root, label):
        self.root = root
        self.label = label

    def readline(self, limit):
        line = sys.stdin.buffer.readline(limit)
        if line:
            message = line[REQUEST_DEADLINE_HEADER:] if line.startswith(b"@") else line
            request = json.loads(message)
            if request.get("method") == DRAIN_METHOD:
                marker(self.root, "barrier-" + self.label)
            if request.get("params", {}).get("arguments", {}).get("action") == "remaining-budget":
                marker(self.root, "received-remaining-budget-" + self.label)
        return line


def worker_main(root, label, protocol):
    marker(root, "spawn-" + label)
    if label == "failed":
        return 4
    if label == "timeout":
        time.sleep(10)
        return 4
    server = FixtureServer(root, label, protocol)
    write_response(sys.stdout, {
        "abWorkerReady": HANDOFF_VERSION,
        "versions": ["2025-06-18"] if label == "protocol-mismatch" else [PROTOCOL],
        "capabilities": {"tools": {}},
        "toolContract": "fixture-v1",
        "cacheIdentity": "fixture-cache",
        "cacheAbi": CONTENT_CACHE_ABI,
        "tools": server.catalog,
    })
    if label == "staged-exit":
        wait_until(lambda: (root / "exit-staged").exists(), timeout=12)
        return 5
    serve(server, FixtureInput(root, label), sys.stdout, managed_stdio=True)
    return 0


def broker_main(root):
    def spec(label, fingerprint, cache_abi):
        return WorkerSpec(
            (sys.executable, "-B", __file__, "worker", str(root), label),
            fingerprint,
            cache_abi,
        )

    previous = None

    def observe():
        nonlocal previous
        try:
            record = (root / "pointer.json").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        if record == previous:
            return None
        previous = record
        return spec(*json.loads(record))

    options = json.loads((root / "supervisor-options.json").read_text(encoding="utf-8"))
    return supervise_stdio(
        spec("old", "old", CONTENT_CACHE_ABI), observe, sys.stdin.buffer, sys.stdout, {},
        poll_interval=0.02, probe_timeout=3,
        can_switch=lambda: not (root / "registration-changed").exists(), **options,
    )


class BrokerTestCase(unittest.TestCase):
    supervisor_options = {}
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "supervisor-options.json").write_text(
            json.dumps(self.supervisor_options), encoding="utf-8",
        )
        env = dict(os.environ)
        home = self.root / "home"
        home.mkdir()
        codex = self.root / "codex"
        codex.mkdir()
        env.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(codex))
        logs = (self.root / "broker.stderr").open("w+b")
        self.addCleanup(logs.close)
        with_logs = subprocess.Popen(
            [sys.executable, "-B", __file__, "broker", str(self.root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=logs, env=env,
        )
        self.broker = with_logs
        self.addCleanup(self.stop_broker)
        self.output = Queue()
        self.buffered = {}

        def read():
            for line in self.broker.stdout:
                response = json.loads(line)
                if "id" in response:
                    self.output.put(response)
            self.output.put(None)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        self.await_marker("spawn-old")
        self.send("initialize", 1, INIT)
        self.assertEqual(self.receive(1)["result"]["protocolVersion"], PROTOCOL)
        self.send("notifications/initialized")
        self.send("tools/list", 2)
        self.original = self.receive(2)["result"]

    def stop_broker(self):
        marker(self.root, "release")
        marker(self.root, "release-new")
        marker(self.root, "exit-staged")
        self.broker.stdin.close()
        try:
            self.broker.wait(timeout=6)
        except subprocess.TimeoutExpired:
            self.broker.kill()
            self.broker.wait(timeout=6)
        self.reader.join(timeout=6)
        self.broker.stdout.close()

    def send(self, method, rid=None, params=None):
        value = {"jsonrpc": "2.0", "method": method}
        if rid is not None:
            value["id"] = rid
        if params is not None:
            value["params"] = params
        self.broker.stdin.write(json.dumps(value).encode("utf-8") + b"\n")
        self.broker.stdin.flush()

    def receive(self, rid):
        if rid in self.buffered:
            return self.buffered.pop(rid)
        try:
            while True:
                value = self.output.get(timeout=8)
                self.assertIsNotNone(value, "Host stdio connection ended")
                if value["id"] == rid:
                    return value
                self.assertNotIn(value["id"], self.buffered)
                self.buffered[value["id"]] = value
        except Empty:
            self.fail("No response before the fixture deadline")

    def call(self, rid, action="version"):
        self.send("tools/call", rid, {"name": "fixture", "arguments": {"action": action}})

    def activate(self, label, fingerprint=None, cache=CONTENT_CACHE_ABI):
        staging = self.root / "pointer.next"
        staging.write_text(json.dumps([label, fingerprint or label, cache]), encoding="utf-8")
        staging.replace(self.root / "pointer.json")

    def await_marker(self, name):
        wait_until(lambda: (self.root / name).exists())

    def await_diagnostic(self, code):
        wait_until(lambda: code in (self.root / "broker.stderr").read_text(encoding="utf-8"))


class HandoffTests(BrokerTestCase):
    def test_same_connection_drains_queue_routes_cancellation_and_refreshes_catalog(self):
        self.call(3, "hold")
        self.await_marker("holding-old")
        self.call(4, "queued")
        self.call("cancel-old", "queued")
        self.activate("new")
        self.await_marker("barrier-old")
        self.call("cancel-held")
        for rid in ("cancel-old", "cancel-held"):
            self.send("notifications/cancelled", params={"requestId": rid})
            self.assertEqual(self.receive(rid)["error"]["code"], -32800)
        self.call(5)
        marker(self.root, "release")
        for rid, label in ((3, "old"), (4, "old"), (5, "new")):
            self.assertEqual(self.receive(rid)["result"]["worker"], label)
        self.await_marker("queued-old")
        self.assertFalse((self.root / "initialized-new").exists())
        self.send("tools/list", 6)
        self.assertEqual(self.receive(6)["result"]["tools"][0]["description"], "Worker new")

    def test_request_id_can_be_reused_and_cancelled_on_new_generation(self):
        self.call("shared-id")
        self.assertEqual(self.receive("shared-id")["result"]["worker"], "old")
        self.call(3, "hold")
        self.await_marker("holding-old")
        self.activate("new")
        self.await_marker("barrier-old")
        self.call(4, "hold-new")
        marker(self.root, "release")
        self.assertEqual(self.receive(3)["result"]["worker"], "old")
        self.await_marker("holding-new")
        self.call("shared-id", "queued")
        self.send("notifications/cancelled", params={"requestId": "shared-id"})
        self.assertEqual(self.receive("shared-id")["error"]["code"], -32800)
        marker(self.root, "release-new")
        self.assertEqual(self.receive(4)["result"]["worker"], "new")
        self.assertFalse((self.root / "queued-new").exists())

    def test_no_restart_for_unchanged_runtime_and_rejects_failed_candidates(self):
        self.activate("metadata-only", "old")
        time.sleep(0.12)
        self.assertFalse((self.root / "spawn-metadata-only").exists())
        self.activate("incompatible", cache="different")
        self.await_diagnostic("cache-contract-changed")
        self.assertFalse((self.root / "spawn-incompatible").exists())
        self.activate("schema")
        self.await_diagnostic("worker-readiness-rejected")
        self.activate("failed")
        self.await_diagnostic("candidate-exited")
        self.activate("timeout")
        self.await_diagnostic("worker-readiness-timeout")
        self.call(3)
        self.assertEqual(self.receive(3)["result"]["worker"], "old")

    def test_incompatible_protocol_cannot_take_over_the_connection(self):
        self.activate("protocol-mismatch")
        self.await_diagnostic("worker-readiness-rejected")
        self.call(3)
        self.assertEqual(self.receive(3)["result"]["worker"], "old")

    def test_candidate_exit_during_drain_keeps_old_worker(self):
        self.call(3, "hold")
        self.await_marker("holding-old")
        self.activate("staged-exit")
        self.await_marker("barrier-old")
        self.call(4)
        marker(self.root, "exit-staged")
        self.await_diagnostic("candidate-exited")
        marker(self.root, "release")
        for rid in (3, 4):
            self.assertEqual(self.receive(rid)["result"]["worker"], "old")

    def test_registration_change_during_drain_keeps_old_connection(self):
        self.call(3, "hold")
        self.await_marker("holding-old")
        self.activate("new")
        self.await_marker("barrier-old")
        self.call(4)
        marker(self.root, "registration-changed")
        marker(self.root, "release")
        self.assertEqual(self.receive(3)["result"]["worker"], "old")
        self.await_diagnostic("native-registration-changed")
        self.assertEqual(self.receive(4)["result"]["worker"], "old")

    def test_unknown_write_is_never_replayed_and_connection_recovers(self):
        self.call(3, "crash")
        self.assertEqual(self.receive(3)["error"]["data"], {
            "outcome": "unknown", "automatic_replay": False,
        })
        self.call(4)
        self.assertEqual(self.receive(4)["result"]["worker"], "old")
        self.assertEqual((self.root / "invocations").read_bytes(), b"crash\n")


class HandoffDeadlineTests(BrokerTestCase):
    supervisor_options = {"request_timeout": 1.5, "drain_timeout": 2.6}

    def test_blocked_barrier_keeps_original_deadline_after_fallback(self):
        self.call(3, "hold")
        self.await_marker("holding-old")
        self.call(4, "queued")
        self.activate("new")
        self.await_marker("barrier-old")
        self.call(5, "queued")
        for rid in (4, 5):
            self.assertEqual(self.receive(rid)["error"], {
                "code": -32001, "message": "request_deadline_exceeded",
                "data": {"stage": "queue"},
            })
        self.assertNotIn(3, self.buffered)
        self.assertFalse((self.root / "queued-old").exists())
        self.call(6, "remaining-budget")
        self.await_diagnostic("worker-drain-timeout")
        self.await_marker("received-remaining-budget-old")
        self.assertEqual(self.receive(6)["error"]["message"], "request_deadline_exceeded")
        self.assertNotIn(3, self.buffered)
        marker(self.root, "release")
        self.assertEqual(self.receive(3)["result"]["worker"], "old")
        self.call(7)
        self.assertEqual(self.receive(7)["result"]["worker"], "old")


class ProductWorkerTests(unittest.TestCase):
    def test_private_probe_and_resume_do_not_refresh_native_mapping(self):
        class Adapter:
            queue_sha = "isolated-cache"

            def methods(self):
                return set()

            def refresh(self):
                raise AssertionError("The candidate may not refresh native mapping")

        worker = ManagedContentServer(Adapter(), "0.8.23", PROTOCOL)
        ready = worker.readiness()
        self.assertEqual(ready["cacheIdentity"], "isolated-cache")
        self.assertEqual(ready["cacheAbi"], CONTENT_CACHE_ABI)
        self.assertIn(PROTOCOL, ready["versions"])
        self.assertEqual(worker.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
        })["tools"], ready["tools"])


def isolated_content_package(root, label, abi, *, fixed="fixed", managed=True, single_entry=False):
    package = root / label
    sources = {
        "src/agent_brain_client/interfaces/mcp/content.py": (
            ("MCP_WORKER_HANDOFF = 1\n" if managed else "") + "# Worker " + label + "\n"
        ),
        "src/agent_brain_client/adapters/storage/content.py": f'CONTENT_CACHE_ABI = "{abi}"\n',
        "src/agent_brain_client/interfaces/mcp/launcher.py": (
            ("MCP_SINGLE_ENTRY_SUPERVISOR = 1\n" if single_entry else "") + "# " + fixed + "\n"
        ),
    }
    manifest = {"files": {}}
    for name, source in sources.items():
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        body = source.encode("utf-8")
        target.write_bytes(body)
        manifest["files"][name] = hashlib.sha256(body).hexdigest()
    (package / "verified.json").write_text(json.dumps({"manifest": manifest}), encoding="utf-8")
    return {
        "package": str(package),
        "version": "0.8.25",
        "manifest_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
    }


class LauncherScopeTests(unittest.TestCase):
    def setUp(self):
        from agent_brain_client.runtime import launcher

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.launcher = launcher
        self.runtime = {"device_home": str(self.home), "content_cache": str(self.home / "cache")}
        self.active = isolated_content_package(self.home, "active", CONTENT_CACHE_ABI)
        self.release = SimpleNamespace(
            policy={"runtime": self.runtime, "capabilities": HTTPS_CONTENT_CAPABILITIES},
            verified_active=Mock(return_value=self.active),
            startup_check=Mock(),
        )

    def start(self, harness, *, event_hook=None):
        with (
            patch.object(self.launcher, "ClientReleases", return_value=self.release),
            patch.object(self.launcher, "require_memory_verified"),
            patch("agent_brain_client.adapters.installation.installation.inspect",
                  return_value={"installed": True}),
            patch("agent_brain_client.interfaces.mcp.server.supervise_stdio") as manager,
            patch.object(self.launcher, "serve_content",
                         side_effect=lambda command, *args: {"command": command}),
        ):
            response = self.launcher.startup(
                "fixture", "a" * 64, harness=harness, event_hook=event_hook,
            )
            return response, manager.call_count

    def test_finite_mcp_hook_uses_original_active_package_and_arguments(self):
        different = isolated_content_package(self.home, "new-abi-hook", "content-ledgers-v2")
        self.release.verified_active.return_value = different
        result, uses_manager = self.start("codex", event_hook=("fixture-hook", "b" * 64))
        self.assertEqual(uses_manager, 0)
        args = result["command"]
        self.assertEqual(args[3], str(Path(different["package"]) / "src/agent_brain_client/interfaces/mcp/content.py"))
        self.assertEqual(args[-5:], [
            "--event-hook", "--event-hook-policy", "fixture-hook",
            "--event-hook-policy-sha256", "b" * 64,
        ])

    def test_ssh_does_not_enable_the_supervisor(self):
        self.release.policy["capabilities"] = SSH_CONTENT_CAPABILITIES
        _, uses_manager = self.start("codex")
        self.assertEqual(uses_manager, 0)


@unittest.skipUnless(sys.platform in ("darwin", "win32"), "Native ownership readers")
class RegistrationStampTests(unittest.TestCase):
    def setUp(self):
        from agent_brain_client.adapters.platforms.filesystem import private_directory
        from agent_brain_client.adapters.storage.private_files import save

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = private_directory(Path(temporary.name).resolve() / "fixture")
        self.state = private_directory(self.home / ".config/agent-brain-client")
        save(self.state / "codex-mcp.json", {"package": "fixture-fixed"})
        self.inspect = patch(
            "agent_brain_client.adapters.installation.installation.inspect",
            return_value={"installed": True},
        )
        self.inspect.start()
        self.addCleanup(self.inspect.stop)

    def test_codex_stamp_reads_mcp_table_without_treating_hook_edits_as_binding_changes(self):
        from agent_brain_client.adapters.harnesses.codex import binding
        from agent_brain_client.adapters.platforms.filesystem import private_directory
        from agent_brain_client.runtime.launcher import registration_stamp

        native = private_directory(self.home / ".codex")
        config = native / "config.toml"
        runtime = {"device_home": str(self.home), "codex_home": str(native)}
        original = f'[mcp_servers.{json.dumps(binding.NAME)}]\nenabled = true\ncommand = "old"\n'
        config.write_text(original, encoding="utf-8")
        stamp = registration_stamp(runtime, "codex")
        self.assertIsNotNone(stamp)
        config.write_text(original + '\n[hooks]\n', encoding="utf-8")
        self.assertEqual(registration_stamp(runtime, "codex"), stamp)
        config.write_text(original.replace('"old"', '"new"'), encoding="utf-8")
        self.assertNotEqual(registration_stamp(runtime, "codex"), stamp)

    def test_codebuddy_stamp_checks_the_owned_entry_and_native_transaction(self):
        from agent_brain_client.adapters.harnesses.codebuddy import configuration as host
        from agent_brain_client.adapters.platforms.filesystem import private_directory
        from agent_brain_client.adapters.storage.private_files import save
        from agent_brain_client.runtime.launcher import registration_stamp

        native = private_directory(self.home / ".codebuddy")
        directory = private_directory(self.state / "codebuddy")
        entry = {"type": "stdio", "command": "old", "args": []}
        config = native / "mcp.json"
        save(directory / "registration.json", {
            "removed": False, "surface": "ide", "mcp_paths": [str(config)], "mcp": entry,
        })
        runtime = {"device_home": str(self.home)}
        config.write_text(json.dumps({"mcpServers": {host.NAME: entry}}), encoding="utf-8")
        stamp = registration_stamp(runtime, "codebuddy")
        self.assertIsNotNone(stamp)
        (native / "settings.json").write_text('{"hooks":{}}', encoding="utf-8")
        self.assertEqual(registration_stamp(runtime, "codebuddy"), stamp)
        save(directory / "transaction.json", {"phase": "applying"})
        self.assertIsNone(registration_stamp(runtime, "codebuddy"))


@unittest.skipUnless(sys.platform == "darwin", "Synthetic Mac first-install binding")
class ManualUpdateFixture(unittest.TestCase):
    shared_control = False

    def setUp(self):
        import platform

        if platform.machine().lower() not in ("arm64", "aarch64"):
            self.skipTest("The existing enrollment fixture is arm64")
        from test_mac_first_install import FirstInstall

        python = Path(sys.executable).resolve(strict=True)
        fixture = FirstInstall("test_first_install_cli_emits_onboard_handoff_without_importing")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fixture()
        if self.shared_control:
            fixture.config.unlink()
            fixture.native.rmdir()
            fixture.harness = "codebuddy"
            fixture.native = fixture.home / ".config/agent-brain-client/shared-native"
            fixture.native.mkdir(mode=0o700)
            fixture.config = fixture.native / "config.toml"
        # Bind executable bytes before first install creates its dependency
        # policy, launcher receipt, or registered command.
        fixture.runtime.stop()
        fixture.materials["dependencies"].update(
            python=str(python), python_sha256=hashlib.sha256(python.read_bytes()).hexdigest(),
        )
        self.assertTrue(fixture.run_wizard()["installed"])
        self.python = str(python)
        self.fixture = fixture
        self.home = fixture.home

    @staticmethod
    def worker_source():
        return b'''MCP_WORKER_HANDOFF = 1
import json
from pathlib import Path
import sys

if sys.argv[1:] == ["--self-check"]:
    print(json.dumps({
        "content_mcp_self_check": "passed",
        "core_imported": False,
        "native_changed": False,
        "keychain_accessed": False,
        "network_used": False,
    }))
    raise SystemExit(0)

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))
if "--background-content" in sys.argv:
    home = Path(sys.argv[sys.argv.index("--device-home") + 1])
    commit = sys.argv[sys.argv.index("--background-commit") + 1]
    probe = home / ("synthetic-background-" + commit)
    probe.touch()
    print(json.dumps({"status": "content-current"}))
    raise SystemExit(0)

from agent_brain_client.adapters.storage.content import CONTENT_CACHE_ABI
from agent_brain_client.interfaces.mcp.framing import write_response
from agent_brain_client.interfaces.mcp.server import DRAIN_METHOD, HANDOFF_VERSION, serve

version = sys.argv[sys.argv.index("--client-version") + 1]
harness = sys.argv[sys.argv.index("--harness") + 1]
seed = sys.argv[sys.argv.index("--managed-stdio") + 1]
protocol = "2025-11-25"
tools = [
    {"name": name, "inputSchema": {"type": "object"}}
    for name in ("content_catalog", "fixture_unregistered")
]

class Worker:
    ready = seed == protocol

    def handle(self, request):
        method = request["method"]
        if method == "initialize":
            return {"protocolVersion": protocol, "capabilities": {"tools": {}}}
        if method == "notifications/initialized":
            self.ready = True
            return None
        if method == DRAIN_METHOD:
            return {}
        if not self.ready:
            raise AssertionError("Initialize first")
        if method == "tools/list":
            return {"tools": tools}
        return {"worker_version": version, "worker_source": str(Path(__file__).resolve()), "harness": harness}

write_response(sys.stdout, {
    "abWorkerReady": HANDOFF_VERSION,
    "versions": [protocol],
    "capabilities": {"tools": {}},
    "toolContract": "fixture-v1",
    "cacheIdentity": "isolated-cache",
    "cacheAbi": CONTENT_CACHE_ABI,
    "tools": tools,
})
serve(Worker(), sys.stdin.buffer, sys.stdout, managed_stdio=True)
'''

    def packages(self, channel, installed, *, replace_launcher=False):
        from agent_brain_client.adapters.packages.client import launcher_package_files
        from agent_brain_client.adapters.packages.releases import digest

        files = launcher_package_files(installed)
        payload = {name: (installed / name).read_bytes() for name in files}
        content = "src/agent_brain_client/interfaces/mcp/content.py"
        old_contents = dict(payload)
        old_contents[content] = self.worker_source()
        next_contents = dict(old_contents)
        next_contents[content] += b"\n# Updated synthetic worker.\n"
        if replace_launcher:
            next_contents["src/agent_brain_client/interfaces/mcp/launcher.py"] += b"\n# Updated fixed supervisor.\n"

        def release(version, data):
            return {
                "schema": 1, "version": version, "channel": channel.policy["channel"],
                "core_api": channel.policy["core_api"], "state_schema": channel.policy["state_schema"],
                "python_min": "3.11", "capabilities": channel.policy["capabilities"],
                "files": {name: digest(body) for name, body in data.items()}, "migrations": [],
            }

        engine = channel.engine
        first = release("0.8.25", old_contents)
        package = engine.prepare("a" * 40, first, old_contents.__getitem__)
        engine.set_active({
            "version": first["version"], "package": str(package), "commit": "a" * 40,
            "manifest_sha256": digest(json.dumps(first, sort_keys=True).encode()),
        })
        latest = release("0.8.26", next_contents)
        engine.source = SimpleNamespace(latest=lambda: ("b" * 40, latest, next_contents.__getitem__))
        return engine.active()

    def start_registered(self, registered):
        self.assertEqual(registered["command"], self.python)
        command = [registered["command"], *registered["args"]]
        environment = {
            "HOME": str(self.home),
            "CODEX_HOME": str(self.home / ".codex" if self.shared_control else self.fixture.native),
            "LANG": "en_US.UTF-8",
            **registered.get("env", {}),
        }
        stderr = (self.home / "managed-update.stderr").open("w+b")
        self.addCleanup(stderr.close)
        child = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, env=environment,
        )
        replies = Queue()

        def receive():
            for raw in child.stdout:
                response = json.loads(raw)
                if response.get("method") == "notifications/tools/list_changed":
                    (self.home / "catalog-notified").touch()
                    continue
                replies.put(response)
            replies.put(None)

        reader = threading.Thread(target=receive, daemon=True)
        reader.start()

        def close():
            if not child.stdin.closed:
                child.stdin.close()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=8)
            reader.join(timeout=8)
            child.stdout.close()

        self.addCleanup(close)
        counter = 0

        def exchange(method, params=None, *, notification=False):
            nonlocal counter
            request = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                request["params"] = params
            if not notification:
                counter += 1
                request["id"] = counter
            child.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
            child.stdin.flush()
            if notification:
                return None
            try:
                response = replies.get(timeout=10)
            except Empty:
                self.fail("The registered stdio connection stopped responding")
            self.assertIsNotNone(response, "The registered stdio connection exited")
            self.assertEqual(response.get("id"), counter)
            return response["result"]

        return child, exchange

    def codebuddy_registration(self, *, surface="cli", replace_launcher=False):
        from agent_brain_client.adapters.harnesses.codebuddy import configuration as host
        from agent_brain_client.adapters.packages.client import ClientReleases
        from agent_brain_client.adapters.packages.frozen import freeze_code, runtime_pins
        from agent_brain_client.adapters.packages.releases import atomic_json, digest
        from agent_brain_client.adapters.platforms.filesystem import (
            cache_directory,
            private_directory,
        )
        from agent_brain_client.adapters.storage import metadata
        from agent_brain_client.adapters.storage.collection import settings_path
        from agent_brain_client.domain.scopes import digest as scope_digest
        from agent_brain_client.runtime.delivery import directory

        state = self.home / ".config/agent-brain-client"
        registration = metadata.private_json(state / "codex-mcp.json")
        channel = ClientReleases(registration["release_policy"], registration["release_policy_sha256"])
        active = self.packages(channel, Path(registration["package"]), replace_launcher=replace_launcher)
        source = Path(active["package"])
        pins = runtime_pins(source)
        settings_file = settings_path(self.home)
        private_directory(settings_file.parent)
        freeze_code(settings_file.parent, pins, source=source)
        scope = {
            "schema": 1, "profile_id": "a" * 32, "include_roots": [], "exclude_roots": [],
            "non_project_roots": [str(self.home)], "recall_personal": True,
        }
        approved = scope_digest(scope)
        metadata.write(settings_file, {
            "schema": 1, "enabled": True, "device_home": str(self.home), "scope": scope,
            "approved_scope_sha256": approved, "server_scope_sha256": approved,
            "legacy_projects": [], "operator": None, "operator_sha256": None, "code_sha256": pins,
        })
        cache_directory(directory(self.home))
        atomic_json(channel.engine.local / "startup-check.json", {
            "policy_sha256": digest(json.dumps(channel.policy, sort_keys=True).encode()),
            "checked_at": time.time(),
        })
        # Start with the migrated shared MCP entry and Hook maintenance pending.
        definitions = host.definitions
        def pending_hooks(entry, home, **_):
            mcp, _ = definitions(entry, home, fixed_hooks=True)
            _, hooks = definitions(entry, home)
            return mcp, hooks

        with patch.object(host, "definitions", side_effect=pending_hooks):
            host.install(self.home, source, None, surface=surface)
        receipt_file = host.state_directory(self.home) / "registration.json"
        receipt = metadata.private_json(receipt_file)
        config = Path(receipt["mcp_paths"][0])
        # Untouched native bytes include a BOM and a user comment.
        config.write_bytes(b"\xef\xbb\xbf" + config.read_bytes().replace(b"{", b"{ // fixture\n", 1))
        return channel, receipt_file, config


class ManualUpdateTests(ManualUpdateFixture):
    """Run the public update and keep talking to the originally registered process."""

    def test_public_update_keeps_fixed_registration_and_switches_existing_stdio(self):
        from agent_brain_client.adapters.installation import installation
        from agent_brain_client.adapters.installation.updates import RegisteredClientInstallation
        from agent_brain_client.adapters.packages.client import ClientReleases
        from agent_brain_client.adapters.packages.releases import atomic_json, digest
        from agent_brain_client.adapters.storage.private_files import private_json
        from agent_brain_client.runtime.host_maintenance import HostMaintenance
        from agent_brain_client.runtime.updates import manage_update

        state = self.home / ".config/agent-brain-client"
        registration_file = state / "codex-mcp.json"
        original = private_json(registration_file)
        _, registered, _, native = installation.registered_context(self.home, original)
        native_config = native / "config.toml"
        native_before = native_config.read_bytes()
        registration_before = registration_file.read_bytes()
        channel = ClientReleases(original["release_policy"], original["release_policy_sha256"])
        old_active = self.packages(channel, Path(original["package"]))
        # The normal check cooldown keeps background discovery out of this
        # fixture. The original launcher command and its checks run intact.
        atomic_json(channel.engine.local / "startup-check.json", {
            "policy_sha256": digest(json.dumps(channel.policy, sort_keys=True).encode()),
            "checked_at": time.time(),
        })
        child, exchange = self.start_registered(registered)
        exchange("initialize", INIT)
        exchange("notifications/initialized", notification=True)
        tools = exchange("tools/list")
        args = {"name": "content_catalog"}
        self.assertEqual(exchange("tools/call", args)["worker_version"], old_active["version"])

        # Hook maintenance has separate coverage. The real public update, release
        # engine, pinned registration and native-launcher comparison run here.
        hooks = {"status": "global-hook-current", "native_changed": False, "update_available": False}
        with (
            patch.object(HostMaintenance, "recover", return_value=False),
            patch.object(HostMaintenance, "refresh", return_value=hooks),
            patch.object(RegisteredClientInstallation, "collection_installed", return_value=True),
            patch("agent_brain_client.runtime.installation_completion.source_setup", return_value={}),
            patch.object(installation, "apply", side_effect=AssertionError("Native rewrite required")),
        ):
            report = manage_update(self.home, "update", channel_factory=lambda *a, **k: channel)
            checked = manage_update(self.home, "check-update", channel_factory=lambda *a, **k: channel)

        self.assertEqual(report["status"], "client-update-updated", report)
        self.assertFalse(report["bootstrap_registration_changed"], report)
        self.assertEqual(checked["status"], "client-update-current", checked)
        self.assertFalse(checked["launcher_update_available"], checked)
        self.assertEqual(registration_file.read_bytes(), registration_before)
        self.assertEqual(native_config.read_bytes(), native_before)
        self.assertFalse((state / "launcher-handover.json").exists())
        active = channel.verified_active()
        self.assertEqual(active["version"], "0.8.26")
        self.assertNotEqual(str(original["package"]), active["package"])
        observed = {}

        def switched():
            observed.update(exchange("tools/call", args))
            return observed["worker_version"] == "0.8.26"

        wait_until(switched, timeout=12)
        self.assertEqual(Path(observed["worker_source"]).parents[4], Path(active["package"]))
        self.assertIsNone(child.poll())
        self.assertEqual(exchange("tools/list"), tools)

    def test_fixed_connection_dispatches_content_without_a_request_and_after_release_change(self):
        from agent_brain_client.adapters.installation import installation
        from agent_brain_client.adapters.packages.client import ClientReleases
        from agent_brain_client.adapters.storage.background import manage_policy
        from agent_brain_client.adapters.storage.private_files import private_json

        metadata = private_json(self.home / ".config/agent-brain-client/codex-mcp.json")
        _, registered, _, _ = installation.registered_context(self.home, metadata)
        channel = ClientReleases(metadata["release_policy"], metadata["release_policy_sha256"])
        old = self.packages(channel, Path(metadata["package"]))
        manage_policy(self.home, "disable")
        child, _ = self.start_registered(registered)
        # The registered fixed launcher starts parked, before any MCP request.
        wait_until(lambda: (self.home / ("synthetic-background-" + old["commit"])).exists())
        self.assertEqual(channel.verified_active(), old)

        installed = channel.update()
        self.assertEqual(installed["status"], "updated", installed)
        commit = installed["active"]["commit"]
        # The original idle connection sees the verified pointer and does not
        # wait for the next periodic tick or require an initialize request.
        wait_until(lambda: (self.home / ("synthetic-background-" + commit)).exists())
        self.assertIsNone(child.poll())

    def run_codebuddy_hook(self, receipt):
        import shlex

        from agent_brain_client.adapters.storage import metadata
        from agent_brain_client.runtime.delivery import directory

        # Refresh the normal spawn cooldown for each isolated invocation.
        metadata.write(directory(self.home) / "spawn.json", {"at": time.time()}, cache=True)
        command = receipt["hooks"]["SessionStart"]["hooks"][0]["command"]
        result = subprocess.run(
            shlex.split(command),
            input=json.dumps({
                "hook_event_name": "SessionStart", "session_id": "native-codebuddy-fixture",
                "cwd": str(self.home),
            }).encode(),
            env={"HOME": str(self.home), "LANG": "en_US.UTF-8"},
            capture_output=True, timeout=15, check=True,
        )
        self.assertEqual(json.loads(result.stdout), {})
        diagnostic = json.loads(result.stderr)
        self.assertEqual(diagnostic["status"], "session-ready", diagnostic)

    def test_codebuddy_public_update_preserves_registered_stdio_and_refreshes_native_hook(self):
        from agent_brain_client.adapters.harnesses.codebuddy import configuration as host
        from agent_brain_client.adapters.packages.frozen import runtime_pins
        from agent_brain_client.adapters.storage import metadata
        from agent_brain_client.adapters.storage.collection import settings_path
        from agent_brain_client.runtime.updates import manage_update

        channel, receipt_file, config = self.codebuddy_registration()
        before = metadata.private_json(receipt_file)
        native_bytes = config.read_bytes()
        child, exchange = self.start_registered(before["mcp"])
        exchange("initialize", INIT)
        exchange("notifications/initialized", notification=True)
        tools = exchange("tools/list")
        args = {"name": "content_catalog"}
        old_worker = exchange("tools/call", args)
        self.assertEqual(old_worker["worker_version"], "0.8.25")
        self.assertEqual(old_worker["harness"], "codebuddy")
        self.run_codebuddy_hook(before)

        report = manage_update(self.home, "update", channel_factory=lambda *a, **k: channel)
        self.assertEqual(report["status"], "client-update-updated", report)
        self.assertFalse(report["bootstrap_registration_changed"], report)
        self.assertTrue(report["global_hook"]["hosts"]["codebuddy"]["native_changed"], report)
        after = metadata.private_json(receipt_file)
        self.assertEqual(after["mcp"], before["mcp"])
        self.assertEqual(config.read_bytes(), native_bytes)
        self.assertEqual(after["hooks_enabled"], before["hooks_enabled"])
        self.assertNotEqual(after["hooks"], before["hooks"])
        native_hooks = host.Document(host.read_host(self.home / ".codebuddy/settings.json")).value["hooks"]
        for event, group in after["hooks"].items():
            self.assertIn(group, native_hooks[event])
            self.assertNotIn(before["hooks"][event], native_hooks[event])
        self.run_codebuddy_hook(after)
        self.run_codebuddy_hook(before)

        seen = {}
        def switched():
            seen.update(exchange("tools/call", args))
            return seen["worker_version"] == "0.8.26"

        wait_until(switched, timeout=15)
        active = Path(channel.verified_active()["package"])
        self.assertEqual(Path(seen["worker_source"]).parents[4], active)
        self.assertEqual(seen["harness"], "codebuddy")
        self.assertIsNone(child.poll())
        self.assertEqual(exchange("tools/list"), tools)
        settings = metadata.private_json(settings_path(self.home))
        self.assertTrue(settings["enabled"])
        self.assertEqual(settings["approved_scope_sha256"], settings["server_scope_sha256"])
        self.assertEqual(settings["code_sha256"], runtime_pins(active))

        # A restarted host can still use the preserved MCP registration.
        _, restarted = self.start_registered(after["mcp"])
        restarted("initialize", INIT)
        restarted("notifications/initialized", notification=True)
        self.assertEqual(restarted("tools/call", args)["worker_version"], "0.8.26")
        checked = manage_update(self.home, "check-update", channel_factory=lambda *a, **k: channel)
        self.assertEqual(checked["status"], "client-update-current", checked)

    def test_codebuddy_hook_transaction_can_roll_back_without_touching_registered_mcp(self):
        from agent_brain_client.adapters.harnesses.codebuddy import configuration as host
        from agent_brain_client.adapters.storage import host_files, metadata
        from agent_brain_client.runtime.launcher import registration_stamp

        channel, receipt_file, config = self.codebuddy_registration()
        before = metadata.private_json(receipt_file)
        native_bytes = config.read_bytes()
        settings = self.home / ".codebuddy/settings.json"
        hook_bytes = settings.read_bytes()
        runtime = {"device_home": str(self.home)}
        stamp = registration_stamp(runtime, "codebuddy")
        self.assertIsNotNone(stamp)
        discovery = channel.discover("codex", channel.policy["platform"], "0.8.25")
        self.assertEqual(discovery["status"], "compatible-update")
        candidate = channel.engine.local / "packages" / ("0.8.26-" + "b" * 40)

        apply = host_files.host_apply
        def applied_then_failed(rows, persist):
            apply(rows, persist)
            self.assertNotIn(str(config), rows)
            self.assertEqual(config.read_bytes(), native_bytes)
            self.assertEqual(registration_stamp(runtime, "codebuddy"), stamp)
            applied = metadata.private_json(receipt_file)
            self.assertNotEqual(applied["hooks"], before["hooks"])
            self.run_codebuddy_hook(applied)
            raise RuntimeError("Synthetic failure after applying native Hook")

        with patch.object(host_files, "host_apply", side_effect=applied_then_failed):
            with self.assertRaisesRegex(RuntimeError, "Synthetic failure"):
                host.refresh(self.home, candidate)
        self.assertEqual(settings.read_bytes(), hook_bytes)
        self.assertEqual(metadata.private_json(receipt_file), before)
        self.assertEqual(config.read_bytes(), native_bytes)
        self.assertEqual(registration_stamp(runtime, "codebuddy"), stamp)
        self.run_codebuddy_hook(before)

        drift = host.Document(native_bytes.decode("utf-8-sig")).patch(
            ("mcpServers", host.NAME, "command"), "/synthetic-outside-edit",
        ).encode()
        config.write_bytes(drift)
        self.assertIsNone(registration_stamp(runtime, "codebuddy"))
        with self.assertRaisesRegex(ValueError, "binding preserved"):
            host.refresh(self.home, candidate)
        self.assertEqual(config.read_bytes(), drift)
        self.assertEqual(settings.read_bytes(), hook_bytes)

    def test_only_changed_host_entry_rewrites_single_entry_registration(self):
        from agent_brain_client.adapters.installation.launcher_update import can_retain_launcher

        root = self.home
        old = isolated_content_package(root, "manual-old", CONTENT_CACHE_ABI, single_entry=True)
        new = isolated_content_package(root, "manual-new", CONTENT_CACHE_ABI, single_entry=True)
        changed_abi = isolated_content_package(root, "manual-different-abi", "content-ledgers-v2", single_entry=True)
        changed_entry = isolated_content_package(root, "manual-different-entry", CONTENT_CACHE_ABI, fixed="changed", single_entry=True)
        entry = "src/agent_brain_client/interfaces/mcp/launcher.py"
        registered = {"args": [str(Path(old["package"]) / entry)], "enabled": True}
        desired = {"args": [str(Path(new["package"]) / entry)], "enabled": True}
        policy = {"capabilities": HTTPS_CONTENT_CAPABILITIES}

        def allowed(snapshot, wanted):
            return can_retain_launcher(policy, old["package"], registered, snapshot["package"], wanted, old)

        self.assertTrue(allowed(new, desired))
        self.assertFalse(allowed(new, dict(desired, enabled=False)))
        self.assertTrue(allowed(changed_abi, dict(desired, args=[str(Path(changed_abi["package"]) / entry)])))
        self.assertFalse(allowed(changed_entry, dict(desired, args=[str(Path(changed_entry["package"]) / entry)])))


class SharedControlStartupTests(ManualUpdateFixture):
    shared_control = True

    def assert_tool_served(self, entry, harness, version="0.8.25"):
        child, exchange = self.start_registered(entry)
        exchange("initialize", INIT)
        exchange("notifications/initialized", notification=True)
        self.assertEqual([tool["name"] for tool in exchange("tools/list")["tools"]], ["content_catalog"])
        result = exchange("tools/call", {"name": "content_catalog"})
        self.assertEqual((result["harness"], result["worker_version"]), (harness, version))
        child.stdin.close()
        self.assertEqual(child.wait(timeout=8), 0)

    def assert_startup_refused(self, entry):
        result = subprocess.run(
            [entry["command"], *entry["args"]], input=b"", capture_output=True, timeout=10,
            env={**entry.get("env", {}), "HOME": str(self.home), "LANG": "en_US.UTF-8"},
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(json.loads(result.stderr.splitlines()[-1])["status"], "verified-startup-unavailable")

    def test_codebuddy_only_uses_shared_catalog_and_accepts_default_switch(self):
        from agent_brain_client.adapters.harnesses.codebuddy import configuration as host

        _, _, config = self.codebuddy_registration(surface="ide")
        self.assertFalse((self.home / ".codex").exists())
        native = host.Document(host.read_host(config))
        entry = native.value["mcpServers"][host.NAME]
        self.assertNotIn("disabled", entry)
        self.assert_tool_served(entry, "codebuddy")
        config.write_text(native.patch(("mcpServers", host.NAME), dict(
            entry, disabled=False, description="IDE metadata",
        )), encoding="utf-8")
        self.assert_tool_served(entry, "codebuddy")
        self.assertFalse((self.home / ".codex").exists())

    def test_each_host_uses_its_own_binding_and_cannot_start_with_disabled_or_changed_command(self):
        from agent_brain_client.adapters.harnesses.codebuddy import configuration as host
        from agent_brain_client.adapters.harnesses.codex import binding
        from agent_brain_client.adapters.storage.toml_document import toml_replace

        _, _, config = self.codebuddy_registration(surface="ide")
        codex_home = self.home / ".codex"
        binding.install(self.home, native=codex_home)
        codex = codex_home / "config.toml"
        self.assertNotEqual(codex.read_bytes(), self.fixture.config.read_bytes())
        entry = binding.entry(codex.read_bytes())
        self.assert_tool_served(entry, "codex")

        def write_codex(value):
            codex.write_text(toml_replace('model = "synthetic"\n', ("mcp_servers", binding.NAME), value))

        default = dict(entry, startup_timeout_sec=70)
        default.pop("enabled")
        write_codex(default)
        self.assert_tool_served(entry, "codex")
        for change in ({"enabled": False}, {"command": "/synthetic/other-python"},
                       {"env": {"CODEX_HOME": "/synthetic/other-home"}}, {"args": ["-I"]},
                       {"default_tools_approval_mode": "approve"}):
            with self.subTest(harness="codex", change=change):
                write_codex(dict(entry, **change))
                self.assert_startup_refused(entry)

        native = host.Document(host.read_host(config))
        buddy = native.value["mcpServers"][host.NAME]
        config.write_text(native.patch(("mcpServers", host.NAME, "disabled"), False))
        self.assert_tool_served(buddy, "codebuddy")
        for change in ({"disabled": True}, {"command": "/synthetic/other-python"},
                       {"env": {"PATH": "/synthetic/other-path"}}, {"args": ["-I"]},
                       {"cwd": str(self.home / "other-cwd")}):
            with self.subTest(harness="codebuddy", change=change):
                config.write_text(native.patch(("mcpServers", host.NAME), dict(buddy, **change)))
                self.assert_startup_refused(buddy)

    def test_public_update_replaces_old_fixed_entry_and_preserves_ide_settings(self):
        from agent_brain_client.adapters.harnesses.codebuddy import configuration as host
        from agent_brain_client.adapters.storage import metadata
        from agent_brain_client.runtime.updates import manage_update

        channel, receipt_file, config = self.codebuddy_registration(surface="ide", replace_launcher=True)
        before = metadata.private_json(receipt_file)["mcp"]
        native = host.Document(host.read_host(config))
        config.write_bytes(b"\xef\xbb\xbf" + native.patch(("mcpServers", host.NAME), dict(
            before, disabled=True, description="Keep this setting",
        )).encode())
        check = manage_update(self.home, "check-update", channel_factory=lambda *args: channel)
        self.assertEqual(check["status"], "client-update-available", check)
        self.assertTrue(check["launcher_update_available"], check)
        report = manage_update(self.home, "update", channel_factory=lambda *args: channel)
        self.assertEqual(report["status"], "client-update-updated", report)
        self.assertTrue(report["bootstrap_registration_changed"], report)
        self.assertTrue(report["global_hook"]["hosts"]["codebuddy"]["native_changes"]["mcp"])
        after = metadata.private_json(receipt_file)["mcp"]
        self.assertNotEqual(before["args"], after["args"])
        self.assertTrue(config.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"// fixture", config.read_bytes())
        updated = host.Document(host.read_host(config))
        self.assertEqual(updated.value["mcpServers"][host.NAME], dict(
            after, disabled=True, description="Keep this setting",
        ))
        self.assert_startup_refused(after)
        config.write_text(updated.patch(("mcpServers", host.NAME, "disabled"), False))
        self.assert_tool_served(after, "codebuddy", "0.8.26")


if __name__ == "__main__":
    mode, root, *args = sys.argv[1:]
    if mode == "broker":
        raise SystemExit(broker_main(Path(root)))
    label, flag, protocol = args
    if flag != "--managed-stdio":
        raise SystemExit(3)
    raise SystemExit(worker_main(Path(root), label, protocol))
