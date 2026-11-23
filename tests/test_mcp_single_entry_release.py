"""Paired release fixture: one transport, two immutable business workers."""
from importlib import import_module
import hashlib
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_brain_client.interfaces.mcp import launcher

from test_mcp_worker_handoff import (
    BrokerTestCase, CONTENT_CACHE_ABI, FixtureInput, FixtureServer, HANDOFF_VERSION,
    INIT, PROTOCOL, marker, serve, wait_until, write_response,
)

FIXTURE_A = '''def business():
    return "A"
'''
FIXTURE_B = '''from new_business_file import response

def business():
    return response()
'''


class ReleaseServer(FixtureServer):
    def __init__(self, root, label, protocol):
        super().__init__(root, label, protocol)
        self.generation = label
        self.module = import_module("business")

    def handle(self, request):
        action = request.get("params", {}).get("arguments", {}).get("action")
        if action in ("write", "unknown-write"):
            with (self.root / "write-invocations").open("ab") as history:
                history.write((action + ":" + self.generation + "\n").encode())
            if action == "unknown-write":
                os._exit(9)
            marker(self.root, "writing-" + self.generation)
            wait_until(lambda: (self.root / "release").is_file())
        result = super().handle(request)
        if action in ("read", "write"):
            return {"worker": self.generation, "business": self.module.business()}
        return result


def worker_main(root, directory, label, protocol):
    sys.path.insert(0, str(directory))
    server = ReleaseServer(root, label, protocol)
    marker(root, "spawn-" + label)
    if label == "B" and (root / "change-registration-on-B-ready").exists():
        marker(root, "changed-registration")
    if (root / "dynamic-catalog").exists():
        args = {"type": "object", "properties": {"action": {"type": "string"}}}
        worker_tool = dict(server.catalog[0], inputSchema=args)
        server.catalog = [worker_tool, {"name": "disabled_by_binding", "inputSchema": args}]
        if label == "A":
            server.catalog.append({"name": "withdrawn", "inputSchema": args})
            server.catalog.append({"name": "changed", "inputSchema": args})
        if label == "B":
            args["properties"]["optional"] = {"type": "boolean"}
            worker_tool["description"] = "Updated description"
            server.catalog.append({"name": "added", "inputSchema": args})
            server.catalog.append({"name": "changed", "inputSchema": dict(args, required=["optional"])})
    write_response(sys.stdout, {
        "abWorkerReady": HANDOFF_VERSION,
        "versions": [PROTOCOL],
        "capabilities": {"tools": {}},
        "toolContract": "fixture-v1",
        "cacheIdentity": "fixture-cache",
        "cacheAbi": CONTENT_CACHE_ABI,
        "tools": server.catalog,
    })
    # Retain an actual handle to a business file until this worker's stdio closes.
    # Windows cannot rename its containing directory until this handle is released.
    with (directory / "business.py").open("rb"):
        serve(server, FixtureInput(root, label), sys.stdout, managed_stdio=True)
    marker(root, "retired-" + label)
    return 0


BROKER = r"""
import hashlib
import json
from pathlib import Path
import runpy
import sys

root, test_file, cache_abi, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
entry = root / "persistent" / "entry" / "package" / "fixed" / "launcher.py"
if mode == "fixed":
    # No application package path or module import exists in the host process.
    table = runpy.run_path(str(entry))
    WorkerSpec, supervise = table["WorkerSpec"], table["supervise_stdio"]
    assert not any(name.startswith("agent_brain_client") for name in sys.modules)
else:
    from agent_brain_client.interfaces.mcp.server import WorkerSpec, supervise_stdio as supervise

def spec(label, fingerprint, cache):
    enabled = None
    if (root / "dynamic-catalog").exists():
        enabled = frozenset(("fixture", "withdrawn", "changed", "disabled_by_binding") if label == "A"
                            else ("fixture", "added", "changed"))
    return WorkerSpec(
        (sys.executable, "-B", test_file, "worker", str(root),
         str(root / ("release-" + label)), label), fingerprint, cache,
        *([enabled] if enabled is not None else []),
    )

stamp = None

def observe():
    global stamp
    try:
        raw = (root / "pointer.json").read_bytes()
    except FileNotFoundError:
        return None
    if raw == stamp:
        return None
    stamp = raw
    label, fingerprint, cache = json.loads(raw)
    return spec(label, fingerprint, cache)

(root / "persistent-started").write_text(
    hashlib.sha256(entry.read_bytes()).hexdigest(), encoding="ascii",
)

def registration_current():
    changed = root / "changed-registration"
    if changed.exists():
        changed.unlink()
        return False
    return True

status = supervise(
    spec("A", "A", cache_abi), observe, sys.stdin.buffer, sys.stdout, {},
    poll_interval=0.02, probe_timeout=3,
    **({"idle_timeout": 0.35, "can_switch": registration_current} if mode == "fixed" else {}),
    **({"dynamic_catalog": True} if (root / "dynamic-catalog").exists() else {}),
)
if mode == "fixed":
    assert not any(name.startswith("agent_brain_client") for name in sys.modules)
(root / "persistent-exited").touch()
raise SystemExit(status)
"""


class SingleEntryFixture(BrokerTestCase):
    dynamic_catalog = False

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        if self.dynamic_catalog:
            marker(self.root, "dynamic-catalog")
        for label, body in (("A", FIXTURE_A), ("B", FIXTURE_B), ("rollback", FIXTURE_A)):
            release = self.root / ("release-" + label)
            release.mkdir()
            (release / "business.py").write_text(body, encoding="utf-8")
        (self.root / "release-B/new_business_file.py").write_text(
            'def response():\n    return "B new file"\n', encoding="utf-8",
        )
        entry_bytes = Path(launcher.__file__).read_bytes()
        anchor_dir = self.root / "persistent" / "entry" / "package" / "fixed"
        anchor_dir.mkdir(parents=True)
        self.entry = anchor_dir / "launcher.py"
        self.entry.write_bytes(entry_bytes)
        for label in ("A", "B", "rollback"):
            (self.root / ("release-" + label) / "launcher.py").write_bytes(entry_bytes)
        home, codex = self.root / "home", self.root / "codex"
        home.mkdir()
        codex.mkdir()
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(codex))
        logs = (self.root / "broker.stderr").open("w+b")
        self.addCleanup(logs.close)
        fixed = hasattr(launcher, "supervise_stdio")
        if os.environ.get("AB_REQUIRE_SINGLE_ENTRY") == "1":
            self.assertTrue(fixed, "Production launcher must expose its stdlib supervisor")
        broker = self.root / "broker.py"
        broker.write_text(BROKER, encoding="utf-8")
        self.broker = subprocess.Popen(
            [sys.executable, *(["-I"] if fixed else []), "-B", str(broker),
             str(self.root), __file__, CONTENT_CACHE_ABI, "fixed" if fixed else "baseline"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=logs, env=env,
        )
        self.addCleanup(
            lambda: self.assertTrue((self.root / "persistent-exited").is_file(),
                                    "Persistent entry should shut down without importing business"))
        self.addCleanup(self.stop_broker)
        self.output, self.buffered, self.notifications = Queue(), {}, Queue()

        def read():
            for line in self.broker.stdout:
                response = json.loads(line)
                if "method" in response:
                    self.notifications.put(response)
                else:
                    self.output.put(response)
            self.output.put(None)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        self.send("initialize", 1, INIT)
        self.await_marker("spawn-A")
        initialized = self.receive(1)["result"]
        self.assertEqual(initialized["protocolVersion"], PROTOCOL)
        if self.dynamic_catalog:
            self.assertTrue(initialized["capabilities"]["tools"]["listChanged"])
        self.send("notifications/initialized")
        self.send("tools/list", 2)
        self.catalog = self.receive(2)["result"]



class SingleEntryPair(SingleEntryFixture):
    def test_ordinary_release_adds_module_drains_and_rolls_back_on_same_connection(self):
        fixed_identity = (self.root / "persistent-started").read_bytes()
        broker_pid = self.broker.pid
        self.assertEqual(self.entry.read_bytes(), (self.root / "release-A/launcher.py").read_bytes())
        self.assertEqual(self.entry.read_bytes(), (self.root / "release-B/launcher.py").read_bytes())
        self.assertEqual(hashlib.sha256(self.entry.read_bytes()).hexdigest().encode(), fixed_identity)
        self.call(3, "write")
        self.await_marker("writing-A")
        self.activate("B")
        self.await_marker("barrier-A")
        if hasattr(launcher, "supervise_stdio"):
            self.assertFalse((self.root / "spawn-B").exists(), "Business workers must not overlap")
        self.call(4, "read")
        marker(self.root, "release")
        self.assertEqual(self.receive(3)["result"], {"worker": "A", "business": "A"})
        self.assertEqual(self.receive(4)["result"], {"worker": "B", "business": "B new file"})
        self.await_marker("retired-A")
        retired = self.root / "release-A"
        removed = self.root / "removed-A"
        retired.rename(removed)
        removed.rename(retired)  # Preserve verified rollback bytes after the handle test.
        self.assertEqual((self.root / "write-invocations").read_bytes(), b"write:A\n")
        self.activate("incompatible", cache="incompatible-business-contract")
        self.await_diagnostic("cache-contract-changed")
        self.assertFalse((self.root / "spawn-incompatible").exists())
        self.call(5, "read")
        self.assertEqual(self.receive(5)["result"]["worker"], "B")
        self.call(6, "unknown-write")
        self.assertEqual(self.receive(6)["error"]["data"],
                         {"outcome": "unknown", "automatic_replay": False})
        self.activate("rollback")
        requests = iter(range(10, 100))
        def rolled_back():
            rid = next(requests)
            self.call(rid, "read")
            return self.receive(rid)["result"] == {"worker": "rollback", "business": "A"}

        wait_until(rolled_back)
        self.await_marker("spawn-rollback")
        self.assertEqual((self.root / "write-invocations").read_bytes(),
                         b"write:A\nunknown-write:B\n")
        self.send("tools/list", 8)
        self.assertEqual(self.receive(8)["result"], self.catalog)
        self.assertEqual(self.broker.pid, broker_pid)
        self.assertEqual((self.root / "persistent-started").read_bytes(), fixed_identity)

    @unittest.skipUnless(hasattr(launcher, "supervise_stdio"), "Requires the new fixed entry")
    def test_idle_releases_business_before_next_release_or_call(self):
        self.call(3, "read")
        self.assertEqual(self.receive(3)["result"], {"worker": "A", "business": "A"})
        self.await_marker("retired-A")
        old = self.root / "release-A"
        released = self.root / "renamed-idle-A"
        old.rename(released)
        released.rename(old)
        self.activate("B")
        time.sleep(0.15)
        self.assertFalse((self.root / "spawn-B").exists(), "Idle approval must not load B business")
        self.call(4, "read")
        self.assertEqual(self.receive(4)["result"], {"worker": "B", "business": "B new file"})
        self.send("tools/list", 5)
        self.assertEqual(self.receive(5)["result"], self.catalog)

    def test_new_worker_readiness_rechecks_native_registration_before_dispatch(self):
        marker(self.root, "change-registration-on-B-ready")
        self.call(3, "write")
        self.await_marker("writing-A")
        self.activate("B")
        self.await_marker("barrier-A")
        self.call(4, "read")
        marker(self.root, "release")
        self.assertEqual(self.receive(3)["result"]["worker"], "A")
        self.await_marker("spawn-B")
        self.await_diagnostic("native-registration-changed")
        self.assertEqual(self.receive(4)["result"]["worker"], "A")


class DynamicSingleEntryPair(SingleEntryFixture):
    dynamic_catalog = True

    def test_new_catalog_preserves_cached_calls_and_drains_in_flight_work(self):
        self.call(3, "write")
        self.await_marker("writing-A")
        self.activate("B")
        self.await_marker("barrier-A")
        self.call(4, "read")  # The host still has the earlier tool list.
        marker(self.root, "release")
        self.assertEqual(self.receive(3)["result"]["worker"], "A")
        self.assertEqual(self.receive(4)["result"]["worker"], "B")
        self.assertEqual(self.notifications.get(timeout=8)["method"], "notifications/tools/list_changed")
        self.send("tools/list", 5)
        listed = {tool["name"]: tool for tool in self.receive(5)["result"]["tools"]}
        self.assertEqual(set(listed), {"fixture", "added", "changed"})
        self.assertEqual(listed["fixture"]["description"], "Updated description")
        self.assertIn("optional", listed["fixture"]["inputSchema"]["properties"])
        self.send("tools/call", 6, {"name": "withdrawn"})
        self.assertEqual(self.receive(6)["error"]["code"], -32602)
        self.send("tools/call", 7, {"name": "changed", "arguments": {"action": "read"}})
        self.assertEqual(self.receive(7)["error"]["code"], -32602)
        self.send("tools/call", 8, {"name": "changed", "arguments": {"action": "read", "optional": True}})
        self.assertEqual(self.receive(8)["result"]["worker"], "B")
        self.send("tools/call", 9, {"name": "disabled_by_binding"})
        self.assertEqual(self.receive(9)["error"]["code"], -32602)
        self.assertIsNone(self.broker.poll())

    def test_parked_connection_wakes_with_the_new_catalog(self):
        self.await_marker("retired-A")
        self.assertIsNone(self.broker.poll())
        self.activate("B")
        time.sleep(0.15)
        self.assertFalse((self.root / "spawn-B").exists())
        self.call(3, "read")
        self.assertEqual(self.receive(3)["result"]["worker"], "B")
        self.assertEqual(self.notifications.get(timeout=8)["method"], "notifications/tools/list_changed")
        self.assertIsNone(self.broker.poll())


@unittest.skipUnless(sys.platform == "darwin", "Uses the native arm64 enrollment fixture")
class PublishedMacMigration(unittest.TestCase):
    """Exercise the registered command with an exact public predecessor."""

    def test_exact_0827_migration_and_business_update(self):
        import platform
        import shlex

        from agent_brain_client.adapters.installation import installation, launcher_update
        from agent_brain_client.adapters.packages.client import ClientReleases
        from agent_brain_client.adapters.packages.releases import GitReleaseSource, atomic_json, digest
        from agent_brain_client.adapters.platforms.filesystem import cache_locked
        from agent_brain_client.adapters.storage import private_files
        from agent_brain_client.runtime import installation as wizard
        from agent_brain_client.runtime.updates import manage_update
        from dev.build.package import export
        from test_mac_first_install import FirstInstall
        from test_mcp_worker_handoff import ManualUpdateTests

        if platform.machine().lower() != "arm64":
            self.skipTest("The existing enrollment fixture is arm64")
        source = Path(__file__).resolve().parents[1]
        git = ["/usr/bin/git", "-C", str(source)]
        prior_commit = "93f414cbc1d204b49ab6508d06708d9b0e27c5b3"
        if subprocess.run([*git, "cat-file", "-e", prior_commit], capture_output=True).returncode:
            self.skipTest("Fetch the official Mac 0.8.27 release object")

        def old_file(name):
            return subprocess.check_output([*git, "show", f"{prior_commit}:{name}"])

        prior = (prior_commit, json.loads(old_file("releases/manifest.json")), old_file)
        fixture = FirstInstall("test_first_install_cli_emits_onboard_handoff_without_importing")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fixture()
        fixture.runtime.stop()
        self.fixture, self.home = fixture, fixture.home
        self.python = str(Path(sys.executable).resolve(strict=True))
        identity = self.home / "synthetic-mcp-identity.json"
        helper = Path(fixture.materials["credential_helper"])
        helper.write_text("#!/bin/sh\nexec /bin/cat " + shlex.quote(str(identity)) + "\n")
        fixture.materials["helper_sha256"] = digest(helper.read_bytes())
        fixture.materials["dependencies"].update(
            python=self.python, python_sha256=digest(Path(self.python).read_bytes()),
        )

        def exact_bootstrap(_source, output, **_options):
            for name in (*prior[1]["files"], "export-manifest.json"):
                target = output / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(old_file(name))

        with (
            patch.object(GitReleaseSource, "latest", return_value=prior),
            patch.object(wizard, "export", side_effect=exact_bootstrap),
        ):
            installed = fixture.run_wizard(updater=None)
        self.assertTrue(installed["installed"], installed)
        private_files.save(identity, {"record": fixture.backend.record})
        state = self.home / ".config/agent-brain-client"
        registry = state / "codex-mcp.json"
        original = private_files.private_json(registry)
        _, old_command, _, native = installation.registered_context(self.home, original)
        channel = ClientReleases(original["release_policy"], original["release_policy_sha256"])
        old = channel.verified_active()
        self.assertEqual((old["version"], old["commit"]), ("0.8.27", prior_commit))
        atomic_json(channel.engine.local / "startup-check.json", {
            "policy_sha256": digest(json.dumps(channel.policy, sort_keys=True).encode()),
            "checked_at": time.time(),
        })

        bundle = fixture.base / "candidate"
        files = export(source, bundle, https_content_candidate=True)["files"]
        base = {name: (bundle / name).read_bytes() for name in files}
        content = "src/agent_brain_client/interfaces/mcp/content.py"
        entry = "src/agent_brain_client/interfaces/mcp/launcher.py"
        extra = "src/agent_brain_client/application/content/mcp_release_fixture.py"

        def update(version, commit, payload):
            manifest = dict(prior[1], version=version, files={
                name: digest(body) for name, body in payload.items()
            })
            channel.engine.source = SimpleNamespace(latest=lambda: (commit, manifest, payload.__getitem__))
            value = manage_update(self.home, "update", channel_factory=lambda *a, **k: channel)
            self.assertEqual(value["status"], "client-update-updated", value)
            return value, channel.verified_active()

        hook_command = launcher_update.native_hook_command(self.home, "codex")
        report, first = update("0.8.28", "a" * 40, base)
        self.assertTrue(report["bootstrap_registration_changed"], report)
        self.assertFalse((state / "launcher-handover.json").exists())
        migrated = private_files.private_json(registry)
        self.assertEqual(migrated["package"], first["package"])
        self.assertEqual(migrated["release_policy_sha256"], original["release_policy_sha256"])
        self.assertTrue(installation.inspect(self.home)["registration_verified"])
        self.assertEqual(fixture.prompts, 1)
        _, command, _, _ = installation.registered_context(self.home, migrated)
        self.assertNotEqual(old_command["args"], command["args"])
        self.assertEqual(hook_command, launcher_update.native_hook_command(self.home, "codex"))
        hook = subprocess.run(
            shlex.split(hook_command), cwd=self.home, timeout=45, check=True,
            input='{"hook_event_name":"SessionStart","session_id":"synthetic-migration"}',
            text=True, capture_output=True,
        )
        self.assertIsInstance(json.loads(hook.stdout), dict)
        self.assertNotIn("Hook launch unavailable", hook.stderr)
        self.assertNotIn("Hook bootstrap verification failed", hook.stderr)
        native_bytes, registry_bytes = (native / "config.toml").read_bytes(), registry.read_bytes()

        child, exchange = ManualUpdateTests.start_registered(self, command)
        exchange("initialize", INIT)
        exchange("notifications/initialized", notification=True)
        catalog = exchange("tools/list")
        self.assertTrue(catalog["tools"])

        def worker_pid(active):
            target = str(Path(active["package"]) / content)
            listing = subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid=,command="], text=True)
            for row in listing.splitlines():
                fields = row.split(None, 2)
                if len(fields) == 3 and fields[1] == str(child.pid) and target in fields[2]:
                    return int(fields[0])
            return None

        first_pid = worker_pid(first)
        self.assertIsNotNone(first_pid)
        lease = Path(channel.policy["runtime"]["content_cache"]) / ".content-runtime.lock"
        with self.assertRaises(RuntimeError):
            with cache_locked(lease, timeout=0):
                self.fail("Active business worker did not retain its lease")
        next_payload = dict(base, **{extra: b"def release_marker():\n    return 'B'\n"})
        old_source = base[content]
        next_payload[content] = old_source.replace(
            b"    def handle(self, request):\n",
            b"    def handle(self, request):\n"
            b"        if request.get('method') == 'ping':\n"
            b"            from agent_brain_client.application.content.mcp_release_fixture import release_marker\n"
            b"            return {'fixture_release': release_marker()}\n",
            1,
        )
        self.assertNotEqual(next_payload[content], old_source)
        report, second = update("0.8.29", "b" * 40, next_payload)
        self.assertFalse(report["bootstrap_registration_changed"], report)
        self.assertEqual(registry.read_bytes(), registry_bytes)
        self.assertEqual((native / "config.toml").read_bytes(), native_bytes)
        self.assertEqual((Path(first["package"]) / entry).read_bytes(), (Path(second["package"]) / entry).read_bytes())
        wait_until(lambda: exchange("ping") == {"fixture_release": "B"}, timeout=20)
        second_pid = worker_pid(second)
        self.assertIsNotNone(second_pid)
        self.assertNotEqual(first_pid, second_pid)
        with self.assertRaises(ProcessLookupError):
            os.kill(first_pid, 0)
        updated_catalog = exchange("tools/list")
        self.assertEqual(
            launcher.tool_contract(updated_catalog["tools"]), launcher.tool_contract(catalog["tools"]),
        )
        self.assertNotEqual(updated_catalog, catalog)
        catalog = updated_catalog

        def authority(mode, ticket=None):
            argv = [command["command"], *command["args"], "--private-stdio", mode]
            if ticket is not None:
                argv += ["--private-ticket", json.dumps(ticket)]
            return subprocess.run(argv, env=dict(os.environ, **command.get("env", {})),
                                  stdin=subprocess.DEVNULL, capture_output=True, timeout=20)

        plan = json.loads(authority("plan").stdout)
        forged = dict(plan["ticket"], active=dict(second, package=str(fixture.base)))
        self.assertNotEqual(authority("resolve", forged).returncode, 0)
        self.assertNotEqual(authority("resolve", dict(plan["ticket"], active=old)).returncode, 0)
        self.assertEqual(authority("resolve", dict(plan["ticket"], active=first)).returncode, 0)

        rejected = dict(next_payload)
        rejected[content] = rejected[content].replace(
            b'"toolContract": TOOL_CONTRACT_VERSION,',
            b'"toolContract": TOOL_CONTRACT_VERSION + 1,',
        )
        self.assertNotEqual(rejected[content], next_payload[content])
        report, third = update("0.8.30", "c" * 40, rejected)
        self.assertFalse(report["bootstrap_registration_changed"], report)
        diagnostic = self.home / "managed-update.stderr"
        def serving_predecessor():
            self.assertEqual(exchange("ping"), {"fixture_release": "B"})
            return b'"error_code": "worker-readiness-rejected"' in diagnostic.read_bytes()

        wait_until(serving_predecessor, timeout=20)
        self.assertIsNone(worker_pid(third))
        fallback_pid = worker_pid(second)
        self.assertIsNotNone(fallback_pid)
        durable_paths = [identity, registry, native / "config.toml", lease.parent / "owned.json",
                         lease.parent / "imports.json", *sorted((lease.parent / "outbox").glob("*.json"))]
        durable_before = {path: private_files.raw(path) for path in durable_paths}

        # Let the known worker unload before the next release overwrites its
        # journal predecessor. The original connection must still resume B.
        wait_until(lambda: worker_pid(second) is None, timeout=40)
        with cache_locked(lease, timeout=0):
            pass
        later = dict(rejected)
        later[content] += b"\n# Second incompatible release.\n"
        report, fourth = update("0.8.31", "d" * 40, later)
        self.assertFalse(report["bootstrap_registration_changed"], report)
        journal = json.loads(channel.engine.journal.read_bytes())
        self.assertEqual((journal["before"], journal["target"]), (third, fourth))
        previous_ticket = dict(plan["ticket"], active=second)
        self.assertNotEqual(authority("resolve", previous_ticket).returncode, 0)
        self.assertNotEqual(authority("verify-retained", forged).returncode, 0)
        verified = authority("verify-retained", previous_ticket)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(set(json.loads(verified.stdout)), {"fingerprint", "cache_abi"})

        rejection = b'"error_code": "worker-readiness-rejected"'
        def serving_after_journal_moves():
            self.assertEqual(exchange("ping"), {"fixture_release": "B"})
            return diagnostic.read_bytes().count(rejection) == 2

        wait_until(serving_after_journal_moves, timeout=20)
        self.assertEqual(exchange("tools/list"), catalog)
        self.assertIsNone(worker_pid(fourth))
        with self.assertRaises(ProcessLookupError):
            os.kill(fallback_pid, 0)
        fallback_pid = worker_pid(second)
        self.assertIsNotNone(fallback_pid)
        self.assertEqual({path: private_files.raw(path) for path in durable_paths}, durable_before)
        self.assertEqual(channel.verified_active(), fourth)
        self.assertEqual(registry.read_bytes(), registry_bytes)
        self.assertEqual((native / "config.toml").read_bytes(), native_bytes)

        # The new supervisor is already running. Update an authorized tool
        # addition through the public registration transaction.
        tools_source = "src/agent_brain_client/application/tools/catalog.py"
        additive = dict(next_payload)
        additive[tools_source] = additive[tools_source].replace(
            b"    return tools\n",
            b'    tools.append({"name": "fixture_added", "inputSchema": {"type": "object"}})\n'
            b"    return tools\n",
            1,
        )
        self.assertNotEqual(additive[tools_source], next_payload[tools_source])
        report, fifth = update("0.8.32", "e" * 40, additive)
        self.assertTrue(report["mcp_registration_changed"], report)
        self.assertEqual(
            Path(private_files.private_json(registry)["package"]), Path(fifth["package"]),
        )
        self.assertEqual(exchange("ping"), {"fixture_release": "B"})
        def current_tools():
            listed = exchange("tools/list")["tools"]
            return {row["name"] for row in listed} - {row["name"] for row in catalog["tools"]}

        try:
            wait_until(lambda: current_tools() == {"fixture_added"}, timeout=12)
        except AssertionError:
            self.fail(diagnostic.read_text(encoding="utf-8"))
        self.assertTrue((self.home / "catalog-notified").exists())
        self.assertIsNotNone(worker_pid(fifth))
        wait_until(lambda: worker_pid(fifth) is None, timeout=40)
        self.assertIsNone(child.poll())
        self.assertEqual(exchange("ping"), {"fixture_release": "B"})
        self.assertIsNotNone(worker_pid(fifth))
        child.stdin.close()
        self.assertEqual(child.wait(timeout=12), 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(fallback_pid, 0)
        with cache_locked(lease, timeout=0):
            pass

        # A host still holding the earlier command also gets the fresh catalog.
        stale, reconnect = ManualUpdateTests.start_registered(self, command)
        reconnect("initialize", INIT)
        reconnect("notifications/initialized", notification=True)
        self.assertIn("fixture_added", {row["name"] for row in reconnect("tools/list")["tools"]})
        self.assertIsNone(stale.poll())


if __name__ == "__main__":
    if sys.argv[1:2] == ["worker"]:
        raise SystemExit(worker_main(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], sys.argv[-1]))
    unittest.main()
