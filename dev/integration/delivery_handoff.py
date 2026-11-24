"""Isolated macOS process regression for Client #299.

Run with PYTHONPATH=src:.:tests python -B -m dev.integration.delivery_handoff
after fetching client-0.8.42-macos and client-0.8.43-macos. The CodeBuddy
case simulates its Windows native-memory layout on Mac. No remote service
or actual host installation is used.
"""

from contextlib import ExitStack
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import time
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import URLError


ROOT = Path(__file__).resolve().parents[2]
ENTRY = "src/agent_brain_client/interfaces/cli/delivery.py"


def child(wrapper, entry, *arguments):
    """Replace only credentials/HTTP and the platform of the source provider."""
    import runpy

    entry = Path(entry)
    sys.path.insert(0, str(entry.resolve().parents[4] / "src"))
    sys.executable = wrapper
    sys.argv = [str(entry), *arguments]
    if entry.name == "launcher.py":
        runpy.run_path(str(entry), run_name="__main__")
        return
    home = Path(arguments[arguments.index("--device-home") + 1])
    version = json.loads((entry.parents[4] / "verified.json").read_bytes())["manifest"]["version"]

    from agent_brain_client.adapters.server import https
    from agent_brain_client.adapters.storage import collection, metadata
    from agent_brain_client.adapters.storage.content_session import ContentSession
    from agent_brain_client.domain.scopes import digest
    from agent_brain_client.runtime import delivery

    record = metadata.private_json(home / "synthetic-credential.json")
    log = home / "wire.jsonl"
    original_init = ContentSession.__init__
    original_view = ContentSession.metadata_view

    def windows_provider(session):
        with patch.object(session, "platform", "win32-x64"):
            session.sources.provider = session.host.sources(session)
        return session

    def init(session, *args, **kwargs):
        original_init(session, *args, **kwargs)
        windows_provider(session)

    def view(*args, **kwargs):
        return windows_provider(original_view(*args, **kwargs))

    class Peer:
        def open(self, request, **_):
            assert request.full_url == "https://synthetic.invalid/v1/requests"
            body = json.loads(request.data)
            method = body["method"]
            result, persistence = {}, {"state": "read-only", "durable": False}
            if method == "memory_global_status":
                scope = metadata.private_json(collection.settings_path(home))["scope"]
                result = dict(global_profile_id=scope["profile_id"], scope_sha256=digest(scope),
                              source_admitted=not (home / "hold-idle").exists())
                (home / ("source-check-" + version)).touch()
            elif method == "memory_global_event_submit":
                event = body["params"]["event"]
                prior = [json.loads(line)["request"] for line in log.read_text().splitlines()] if log.exists() else []
                matching = [row for row in prior if row["request_id"] == body["request_id"]]
                assert all(row == body for row in matching)
                with log.open("a") as stream:
                    stream.write(json.dumps(dict(version=version, request=body)) + "\n")
                if version == "0.8.42" and (home / "hold-send").exists() and not matching:
                    (home / "in-flight").touch()
                    deadline = time.monotonic() + 18
                    while not (home / "finish-send").exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    if (home / "lose-ack").exists():
                        raise URLError("Synthetic lost acknowledgement after commit")
                if version == "0.8.44" and (home / "hold-current").exists():
                    (home / "current-in-flight").touch()
                    deadline = time.monotonic() + 15
                    while not (home / "finish-current").exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                result = dict(event_id=event["event_id"], state="committed", message_ids=["synthetic-l0"],
                              replayed=bool(matching), ledger_sync="sqlite-wal-full",
                              core_acceptance="acknowledged-not-durability-proof",
                              pipeline_state="unverified", model_extraction_verified=False)
                persistence = dict(state="core-accepted", durable=False, github_confirmed=False,
                                   receipt_ledger_durable=True)
            elif method == "content_catalog":
                result = {"items": []}
            else:
                raise AssertionError("Unexpected synthetic method " + method)
            return io.BytesIO(json.dumps(dict(request_id=body["request_id"], result=result,
                                              persistence=persistence)).encode())

    run = delivery.run
    save = metadata.write

    def observed_write(path, value, **kwargs):
        if Path(path).name == "status.json" and isinstance(value, dict) and value.get("error_type"):
            (home / ("delivery-error-" + version)).write_text(traceback.format_exc())
        return save(path, value, **kwargs)

    def observed_run(*args, **kwargs):
        pid = home / ("delivery-pid-" + version)
        pid.write_text(str(os.getpid()))

        def pause(seconds):
            until = time.monotonic() + seconds
            while time.monotonic() < until or (
                version == "0.8.42" and (home / "hold-idle").exists()
            ):
                if (home / "closing-fixture").exists():
                    raise SystemExit(0)
                time.sleep(0.02)

        try:
            return run(*args, pause=pause, **kwargs)
        except Exception:
            (home / ("delivery-error-" + version)).write_text(traceback.format_exc())
            raise
        finally:
            (home / ("delivery-exited-" + version)).touch()

    with ExitStack() as stack:
        stack.enter_context(patch.object(https, "native_store", return_value=SimpleNamespace(load=lambda: record)))
        stack.enter_context(patch.object(https, "RemoteClient", return_value=SimpleNamespace(opener=Peer())))
        stack.enter_context(patch.object(delivery, "run", observed_run))
        stack.enter_context(patch.object(metadata, "write", observed_write))
        if (home / "simulate-windows-layout").exists():
            stack.enter_context(patch.object(ContentSession, "__init__", init))
            stack.enter_context(patch.object(ContentSession, "metadata_view", view))
        runpy.run_path(str(entry), run_name="__main__")


if __name__ == "__main__" and sys.argv[1:2] == ["--fixture-child"]:
    child(*sys.argv[2:])
    raise SystemExit(0)


from agent_brain_client.adapters.installation import installation
from agent_brain_client.adapters.installation.updates import RegisteredClientInstallation
from agent_brain_client.adapters.packages.client import ClientReleases, GitReleaseSource
from agent_brain_client.adapters.packages.frozen import runtime_pins
from agent_brain_client.adapters.packages.releases import atomic_json, digest
from agent_brain_client.adapters.storage import collection_setup, metadata
from agent_brain_client.adapters.storage.collection import settings_path
from agent_brain_client.application.memory.onboarding import SOURCE_SCOPE
from agent_brain_client.domain.authorization import OWNER_SCOPE
from agent_brain_client.domain.scopes import digest as scope_digest
from agent_brain_client.runtime import delivery
from agent_brain_client.runtime.harness_content import content_host
from agent_brain_client.runtime.host_maintenance import HostMaintenance
from agent_brain_client.runtime.updates import manage_update
from test_mac_first_install import FirstInstall
from test_mcp_worker_handoff import INIT, ManualUpdateFixture, wait_until


@unittest.skipUnless(sys.platform == "darwin", "Isolated native Mac subprocesses")
class Handoff(unittest.TestCase):
    start_registered = ManualUpdateFixture.start_registered

    def published(self, version):
        ref = "client-" + version + "-macos"
        commit = subprocess.check_output(["git", "rev-parse", ref + "^{}"], cwd=ROOT, text=True).strip()
        directory = self.fixture.base / ref
        directory.mkdir()
        archive = subprocess.check_output(["git", "archive", ref], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            stream.extractall(directory, filter="data")
        manifest = json.loads((directory / "releases/manifest.json").read_bytes())
        return commit, manifest, lambda name: (directory / name).read_bytes()

    def exercise(self, harness, *, reproduce=False, idle=False, lost_ack=False):
        python = Path(sys.executable).resolve(strict=True)
        fixture = FirstInstall("test_first_install_cli_emits_onboard_handoff_without_importing")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fixture_grant["ids"] = dict.fromkeys(fixture.fixture_grant["artifact_types"], OWNER_SCOPE)
        fixture.code = fixture.authority.issue(fixture.fixture_grant, 300)
        fixture.fixture()
        self.fixture, self.home, self.shared_control = fixture, fixture.home, harness == "codebuddy"
        if self.shared_control:
            fixture.config.unlink()
            fixture.native.rmdir()
            fixture.harness = "codebuddy"
            fixture.native = self.home / ".config/agent-brain-client/shared-native"
            fixture.native.mkdir(mode=0o700)
            fixture.config = fixture.native / "config.toml"
            (self.home / "simulate-windows-layout").touch()

        self.python = fixture.materials["dependencies"]["python"]
        wrapper = Path(self.python)
        wrapper.write_text(
            "#!/bin/sh\n"
            'if [ "$2" = "-S" ]; then arg="$4"; n=3; else arg="$3"; n=2; fi\n'
            'case "$arg" in\n'
            '  */interfaces/mcp/launcher.py|*/interfaces/cli/delivery.py|*/interfaces/mcp/content.py)\n'
            '    case "$*" in *--device-home*|*--policy-file*)\n'
            f'      shift "$n"; exec {shlex.quote(str(python))} -I -B {shlex.quote(str(Path(__file__).resolve()))}'
            ' --fixture-child "$0" "$@" ;;\n'
            '    esac ;;\n'
            'esac\n'
            f'exec {shlex.quote(str(python))} "$@"\n'
        )
        fixture.materials["dependencies"]["python_sha256"] = sha256(wrapper.read_bytes()).hexdigest()
        old = self.published("0.8.42")
        with patch.object(GitReleaseSource, "latest", return_value=old):
            initial = fixture.run_wizard(updater=None)
        self.assertTrue(initial["installed"], initial)
        metadata.write(self.home / "synthetic-credential.json", fixture.backend.record)
        work = self.home / "synthetic-project"
        work.mkdir()
        data = collection_setup.prepare(SimpleNamespace(
            device_home=self.home, profile_id="a" * 32, operator=None, operator_sha256=None), locate=None)
        data["scope"].update(non_project_roots=[str(work)])
        scope = scope_digest(data["scope"])
        state = self.home / ".config/agent-brain-client"
        registration_path = state / "codex-mcp.json"
        registration = metadata.private_json(registration_path)
        channel = ClientReleases(registration["release_policy"], registration["release_policy_sha256"])
        active = channel.verified_active()
        self.assertEqual(active["commit"], old[0])
        native, _ = content_host(harness).location(self.home, channel.policy["runtime"])
        memory = ((self.home / "AppData/Local/CodeBuddyExtension/Data/Public/.memories")
                  if self.shared_control else (native / "memories"))
        memory.mkdir(parents=True)
        (memory / "seed.md").write_text("Synthetic memory from before the upgrade.")
        metadata.write(settings_path(self.home), dict(
            data, enabled=True, approved_scope_sha256=scope, server_scope_sha256=scope,
            code_sha256=runtime_pins(Path(active["package"]))))
        atomic_json(channel.engine.local / "startup-check.json", {
            "policy_sha256": digest(json.dumps(channel.policy, sort_keys=True).encode()), "checked_at": time.time(),
        })
        if self.shared_control:
            from agent_brain_client.adapters.harnesses.codebuddy import configuration

            configuration.install(self.home, Path(active["package"]), None, surface="ide")
            registered = metadata.private_json(configuration.state_directory(self.home) / "registration.json")["mcp"]
        else:
            _, registered, _, _ = installation.registered_context(self.home, registration)

        def finish():
            for name in ("finish-send", "finish-current", "closing-fixture"):
                (self.home / name).touch()
            def all_exited():
                return all((self.home / path.name.replace("-pid-", "-exited-")).exists()
                           for path in self.home.glob("delivery-pid-*"))

            wait_until(all_exited, timeout=10)

        self.addCleanup(finish)
        child_process, exchange = self.start_registered(registered)
        try:
            exchange("initialize", INIT)
        except AssertionError as error:
            self.fail(str(error) + "\n" + (self.home / "managed-update.stderr").read_text())
        exchange("notifications/initialized", notification=True)
        tools = exchange("tools/list")["tools"]
        self.assertIn("memory_onboard", [tool["name"] for tool in tools])
        arguments = dict(harness=harness, source_scope=SOURCE_SCOPE, destination=dict(
            endpoint="https://synthetic.invalid", actor=fixture.fixture_grant["actor"], origin=str(work)))

        def onboard(**extra):
            return exchange("tools/call", {"name": "memory_onboard", "arguments": dict(arguments, **extra)})[
                "structuredContent"
            ]

        def runtime():
            return exchange("tools/call", {
                "name": "content_catalog", "arguments": {"kinds": ["memory"]},
            })["structuredContent"]["client_runtime"]

        (self.home / ("hold-idle" if idle else "hold-send")).touch()
        if lost_ack:
            (self.home / "lose-ack").touch()
        if not reproduce:
            (self.home / "hold-current").touch()
        accepted = onboard(approved=True)
        self.assertIn("approval_id", accepted, accepted)
        self.assertEqual(accepted["counts"]["pending"], 1, accepted)
        root = delivery.directory(self.home)
        expected = self.home / ("source-check-0.8.42" if idle else "in-flight")
        try:
            wait_until(expected.exists, timeout=8)
        except AssertionError:
            progress = onboard(resume=True, approval_id=accepted["approval_id"])
            self.fail(repr((progress, delivery._read(root / "status.json"))) + "\n" + "\n".join(
                path.read_text() for path in self.home.glob("delivery-error-*")
            ))
        self.assertTrue(delivery._held(root / "worker.lock"))
        self.assertFalse((root / "handoff/owner.json").exists())

        published = self.published("0.8.43")

        def fixed_release():
            from dev.build.package import export

            candidate = fixture.base / "candidate"
            report = export(ROOT, candidate, https_content_candidate=True)
            manifest = dict(published[1], version="0.8.44", files=report["files"])
            return "b" * 40, manifest, lambda name: (candidate / name).read_bytes()

        hooks = {"status": "global-hook-current", "native_changed": False, "update_available": False}

        def refresh(update_channel, *, check_only=False):
            active = update_channel.verified_active()
            collection_setup.refresh_code(self.home, Path(active["package"]), check_only=check_only)
            return hooks

        def update(next_release):
            channel.engine.source = SimpleNamespace(latest=lambda: next_release)
            with (
                patch.object(HostMaintenance, "recover", return_value=False),
                patch.object(HostMaintenance, "refresh", side_effect=refresh),
                patch.object(RegisteredClientInstallation, "collection_installed", return_value=True),
                patch("agent_brain_client.runtime.installation_completion.source_setup", return_value={}),
            ):
                report = manage_update(self.home, "update", channel_factory=lambda *a, **k: channel)
            self.assertEqual(report["status"], "client-update-updated", report)
            self.assertEqual(metadata.private_json(registration_path), registration)
            version = channel.verified_active()["version"]
            wait_until(lambda: runtime()["running_worker_version"] == version, timeout=12)
            return version

        next_release = published if reproduce else fixed_release()
        version = update(next_release)
        for index in range(65):
            (memory / f"extra-{index:02}.md").write_text(f"Synthetic original number {index}.")
        paged = onboard(approved=True)
        self.assertIn("approval_id", paged, paged)
        self.assertNotEqual(accepted["approval_id"], paged["approval_id"])
        self.assertEqual(paged["counts"]["pending"], 66, paged)
        if not reproduce:
            observed = {}

            def pending_handoff():
                observed.update(runtime())
                return observed["delivery_worker_status"] == "handoff-pending"

            wait_until(pending_handoff, timeout=4)
            self.assertEqual(observed["active_package_version"], version)
            self.assertIsNone(observed["running_delivery_version"])
            self.assertTrue(any((root / "onboarding").glob("*.json")))
            self.assertTrue(any((root / "handoff/parked").glob("*.json")))

        (self.home / "finish-send").touch()
        (self.home / "hold-idle").unlink(missing_ok=True)
        if reproduce:
            def got_key_error():
                return delivery._read(root / "status.json").get("error_type") == "KeyError"

            wait_until(got_key_error, timeout=12)
            self.assertTrue(delivery._held(root / "worker.lock"))
            self.assertFalse((self.home / "delivery-pid-0.8.43").exists())
            self.assertIsNone(child_process.poll())
            (self.home / "hold-current").touch()
            next_release = fixed_release()
            version = update(next_release)

        wait_until((self.home / "current-in-flight").exists, timeout=12)
        self.assertTrue((self.home / "delivery-exited-0.8.42").exists())
        actual = runtime()
        self.assertEqual(actual["running_delivery_version"], version)
        self.assertEqual(actual["running_delivery_commit"], next_release[0])
        self.assertEqual(actual["delivery_worker_status"], "current")
        (self.home / "finish-current").touch()

        result = {}

        def completed():
            result.update(onboard(resume=True, approval_id=paged["approval_id"]))
            return result["counts"]["pending"] == 0

        wait_until(completed, timeout=15)
        self.assertEqual(result["approval_id"], paged["approval_id"])
        counts = result["counts"]
        self.assertEqual((counts["received"], counts["already_exists"], counts["failed"]), (65, 1, 0))
        old_progress = onboard(resume=True, approval_id=accepted["approval_id"])
        self.assertEqual((old_progress["counts"]["pending"], old_progress["counts"]["failed"]), (0, 0))
        self.assertEqual(old_progress["counts"]["received"] + old_progress["counts"]["already_exists"], 1)
        sent = [json.loads(line) for line in (self.home / "wire.jsonl").read_text().splitlines()]
        unique = {row["request"]["request_id"] for row in sent}
        self.assertEqual((len(unique), len(sent)), (66, 66 + int(lost_ack)))
        worker_posts = [row for row in sent if row["version"] == "0.8.42"]
        self.assertEqual(len(worker_posts), 0 if idle else 1)
        if lost_ack:
            same = [row for row in sent if row["request"] == worker_posts[0]["request"]]
            self.assertEqual([row["version"] for row in same], ["0.8.42", version])
        self.assertFalse(any((root / "handoff/parked").glob("*.json")))
        receipt_root = state / ("global-memory" if harness == "codex" else "codebuddy") / "outboxes"
        receipts = [metadata.private_json(row, cache=True) for row in receipt_root.glob("*/outbox/*.json")]
        self.assertEqual({row["request"]["request_id"] for row in receipts}, unique)
        self.assertTrue(all(row["state"] == "confirmed" for row in receipts))
        failures = {path.name for path in self.home.glob("delivery-error-*")}
        self.assertEqual(failures, {"delivery-error-0.8.42"} if reproduce else set())
        self.assertIsNone(child_process.poll())
        self.assertEqual({row["name"] for row in exchange("tools/list")["tools"]},
                         {row["name"] for row in tools})

    def test_published_windows_layout_reproduces_and_recovers_same_batch(self):
        self.exercise("codebuddy", reproduce=True)

    def test_fixed_windows_layout_legacy_lost_ack(self):
        self.exercise("codebuddy", lost_ack=True)

    def test_fixed_mac_codex_legacy_idle(self):
        self.exercise("codex", idle=True)

    def test_fixed_mac_codex_legacy_in_flight(self):
        self.exercise("codex")


if __name__ == "__main__":
    unittest.main()
