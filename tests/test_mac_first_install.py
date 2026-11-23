import agent_brain_client.adapters.installation.update_interpreter as update_interpreter
import agent_brain_client.adapters.storage.private_files as adapters_storage_private_files
import agent_brain_client.adapters.storage.install_state as adapters_storage_install_state
import agent_brain_client.interfaces.cli.install as interfaces_cli_install
import agent_brain_client.runtime.dependencies as runtime_dependencies
import agent_brain_client.runtime.updates as runtime_updates
"""Synthetic repository first-install transactions; no real UI, Keychain, issuer or server."""

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path[:0] = [
    str(Path(__file__).resolve().parents[1] / name) for name in ("tools", "tests")
]
import test_https_content as fixtures

GRANT, ROOT = fixtures.GRANT, fixtures.ROOT
from agent_brain_client.application.installation.pairing import EnrollmentTerminal
from agent_brain_client.adapters.packages import client as releases
from agent_brain_client.adapters.packages.client import dependency_environment
from agent_brain_client.adapters.packages.frozen import freeze_code
from agent_brain_client.adapters.platforms.credentials import canonical
from agent_brain_client.adapters.storage import collection, collection_setup, metadata
from agent_brain_client.domain.scopes import digest, namespace
import agent_brain_client.runtime.installation as wizard
import agent_brain_client.adapters.installation.installation as content_install


DELIVERY_FIXTURE = """
import io
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.executable, entry, *args = sys.argv[1:]
home = Path(args[args.index('--device-home') + 1])
entry = Path(entry)
sys.path.insert(0, str(entry.parents[3]))
from agent_brain_client.adapters.packages.client import ClientReleases
from agent_brain_client.adapters.server import https
from agent_brain_client.adapters.storage import collection, metadata
from agent_brain_client.domain.scopes import digest
from agent_brain_client.runtime import delivery, host_binding

record = metadata.private_json(home / 'synthetic-credential.json')

class Peer:
    def open(self, request, **kwargs):
        assert request.full_url == 'https://synthetic.invalid/v1/requests'
        assert request.get_header('Authorization') == 'Bearer ' + record['credential']
        body = json.loads(request.data)
        method, params = body['method'], body['params']
        reply = {'request_id': body['request_id']}
        if method == 'memory_global_status':
            scope = metadata.private_json(collection.settings_path(home))['scope']
            reply['result'] = {'global_profile_id': scope['profile_id'],
                               'scope_sha256': digest(scope), 'source_admitted': True}
        elif method == 'memory_global_event_submit':
            event = params['event']
            reply['result'] = {'event_id': event['event_id'], 'state': 'committed',
                'message_ids': ['synthetic-received-message'],
                'core_acceptance': 'acknowledged-not-durability-proof', 'ledger_sync': 'sqlite-wal-full',
                'pipeline_state': 'unverified', 'model_extraction_verified': False}
            reply['persistence'] = {'state': 'core-accepted', 'durable': False,
                'github_confirmed': False, 'receipt_ledger_durable': True}
        else:
            reply['error'] = {'code': 'synthetic_recall_unavailable'}
        with (home / 'synthetic-wire.jsonl').open('a') as log:
            log.write(json.dumps(body) + '\\n')
        return io.BytesIO(json.dumps(reply).encode())

def remote(*args, **kwargs):
    return SimpleNamespace(opener=Peer())

def worker(home):
    with (patch.object(ClientReleases, 'verified_active', autospec=True,
                       side_effect=ClientReleases.verified_active) as verified,
          patch.object(collection, 'runtime_pins', create=True,
                       side_effect=AssertionError('Repeated code check'))):
        try:
            run(home, max_seconds=2)
        finally:
            (home / 'synthetic-worker-finished').write_text(str(verified.call_count))

run = delivery.run
sys.argv = [str(entry), *args]
if '--probe-delivery' in args:
    host_binding.load_delivery_core(home).load(home)
elif '--probe-frozen' in args:
    host_binding.load_core(home)
else:
    with (patch.object(https, 'native_store', return_value=SimpleNamespace(load=lambda: record)),
          patch.object(https, 'RemoteClient', remote), patch.object(delivery, 'run', worker)):
        runpy.run_path(str(entry), run_name='__main__')
"""


class FirstInstall(unittest.TestCase):
    def setUp(self):
        fixtures.HTTPSContent.setUp(self)
        self.machine = patch("platform.machine", return_value="arm64")
        self.machine.start()
        self.addCleanup(self.machine.stop)
        self.fixture_grant = copy.deepcopy(GRANT)
        self.fixture_grant["platform"] = "darwin-arm64"
        self.code = self.authority.issue(self.fixture_grant, 300)

    input = fixtures.HTTPSContent.input

    def test_registered_runtime_handoff_preserves_interactive_update_only(self):
        policy = {'runtime':{k:'synthetic' for k in update_interpreter.DEPENDENCY_RUNTIME}}
        mcp = {'command':str(self.home / 'synthetic-pinned-python')}
        for action, interactive, expected in (('update',True,None),('update',False,subprocess.DEVNULL),
                                               ('check-update',True,subprocess.DEVNULL)):
            with (self.subTest(action=action,interactive=interactive),
                  patch.object(update_interpreter,'dependency_environment',return_value={}),
                  patch.object(sys.stdin,'isatty',return_value=interactive)):
                runner = Mock(return_value=type('Reply',(),{'returncode':0})())
                self.assertEqual(update_interpreter.pinned_update_runtime(self.home,action,policy,mcp,self.home,runner),0)
                self.assertIs(runner.call_args.kwargs['stdin'],expected)

    def fixture(self):
        dependencies = self.home / "tools"
        dependencies.mkdir(mode=0o700)
        python, git = dependencies / "python3", dependencies / "git"
        core = dependencies / "git-core"
        core.mkdir()
        for path in (python, git, core / "git-remote-https"):
            path.write_bytes(b"#!/bin/sh\n# SYNTHETIC_UNEXECUTED_DEPENDENCY\nexit 0\n")
            path.chmod(0o700)
        helper = dependencies / "credential-helper"
        helper.write_bytes(b"#!/bin/sh\n# SYNTHETIC_UNEXECUTED_HELPER\nexit 0\n")
        helper.chmod(0o700)
        self.materials = {
            "source_sha256": hashlib.sha256(
                (ROOT / "src/agent_brain_client/adapters/platforms/macos/native/CredentialStore.swift").read_bytes()
            ).hexdigest(),
            "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
            "credential_helper": str(helper),
            "dependencies": {
                "schema": 2,
                "python": str(python),
                "git": str(git),
                "git_exec_path": str(core),
                "python_sha256": hashlib.sha256(python.read_bytes()).hexdigest(),
                "git_sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
            },
        }
        self.connection = {
            "schema": 1,
            "endpoint": "https://synthetic.invalid",
            "grant": copy.deepcopy(self.fixture_grant),
            "release": {
                "origin": wizard.OFFICIAL_ORIGIN,
                "ref": wizard.OFFICIAL_REF,
                "anchor_commit": "a" * 40,
            },
        }
        connection_path = self.base / "connection.json"
        connection_path.write_bytes(canonical(self.connection))
        self.connection, self.connection_sha = wizard.connection_file(connection_path)
        (self.home / ".config/agent-brain-client/first-install").mkdir(
            parents=True, mode=0o700
        )
        self.native = self.home / "codex"
        self.native.mkdir(mode=0o700)
        self.config = self.native / "config.toml"
        self.config.write_text('model="synthetic"\n')
        self.config.chmod(0o600)
        self.events = []
        self.runtime = patch.object(
            sys, "executable", self.materials["dependencies"]["python"]
        )
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.backend.confirm_reset = lambda *args: True
        return dependencies

    def runner(self, argv, **kwargs):
        self.events.append(("process", argv))
        if "--self-check" in argv:
            stdout = json.dumps(
                {
                    "credential_helper_self_check": "passed",
                    "keychain_accessed": False,
                    "ui_opened": False,
                }
            ).encode()
        elif "get-url" in argv:
            stdout = wizard.OFFICIAL_ORIGIN.encode()
        elif "init" in argv:
            (Path(argv[-1]) / ".git").mkdir(parents=True)
            stdout = b""
        else:
            stdout = b""
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    def update(self):
        self.events.append(("update", None))
        self.assertEqual(self.prompts, 0)
        self.assertIsNone(self.backend.record)
        return {"status": "updated", "active": {"version": "0.2.6"}}

    def run_wizard(self, **kwargs):
        options = {"harness": getattr(self, "harness", "codex"),
            "confirm": lambda *args: "Continue",
            "updater": self.update,
            "runner": self.runner,
            "dependency_preparer": lambda *a, **k: self.materials,
            "store": self.store,
            "transport": self.transport,
            "secure_input": self.input,
            "installer": lambda p, d: content_install.apply(p, d, lambda value: None),
        }
        options.update(kwargs)
        return wizard.run(
            self.home,
            self.native,
            self.connection,
            self.connection_sha,
            **options,
        )

    def installed_delivery_fixture(self):
        python = Path(sys.executable).resolve()
        profile_id = 'a' * 32
        project_id = namespace(self.fixture_grant['actor'], profile_id)
        event_key = digest(['observed-v1', project_id, 'synthetic-session', 'synthetic-turn', 'prompt', 0])
        self.fixture_grant['ids']['memory'][0] = 'evt-observed-' + event_key[:48]
        self.code = self.authority.issue(self.fixture_grant, 300)
        self.fixture()
        driver = self.home / 'synthetic-delivery.py'
        driver.write_text(DELIVERY_FIXTURE)
        interpreter = Path(self.materials['dependencies']['python'])
        interpreter.write_text(
            '#!/bin/sh\n'
            'case "$3" in\n'
            '  */interfaces/cli/delivery.py|*/interfaces/cli/collection.py)\n'
            f'    shift 2; exec {shlex.quote(str(python))} -I -B {shlex.quote(str(driver))} "$0" "$@" ;;\n'
            'esac\n'
            f'exec {shlex.quote(str(python))} "$@"\n'
        )
        self.materials['dependencies']['python_sha256'] = metadata.digest(interpreter.read_bytes())

        candidate = self.base / 'candidate'
        exported = fixtures.export(ROOT, candidate, https_content_candidate=True)
        manifest = dict(schema=1, version='0.8.24', channel='stable', core_api=1,
                        state_schema=1, python_min='3.11', migrations=[],
                        capabilities=releases.HTTPS_CONTENT_CAPABILITIES, files=exported['files'])
        release = ('a' * 40, manifest, lambda member: (candidate / member).read_bytes())
        with patch.object(releases.GitReleaseSource, 'latest', return_value=release):
            result = self.run_wizard(updater=None)
        self.assertTrue(result['installed'], result)
        self.assertTrue(content_install.inspect(self.home)['registration_verified'])
        registration, _ = collection.registration_store.registration(self.home)
        channel = releases.ClientReleases(registration['release_policy'], registration['release_policy_sha256'])
        active = channel.verified_active(serving=True)
        package = Path(active['package'])
        self.assertEqual(package.parent, self.home / '.config/agent-brain-client/first-install/releases/packages')

        value = collection_setup.prepare(SimpleNamespace(
            device_home=self.home, profile_id=profile_id, operator=None, operator_sha256=None), locate=None)
        value['scope']['all_projects'] = True
        scope_sha = digest(value['scope'])
        metadata.write(collection.settings_path(self.home), dict(
            value, enabled=True, approved_scope_sha256=scope_sha, server_scope_sha256=scope_sha))
        metadata.write(self.home / 'synthetic-credential.json', self.backend.record)
        self.assertFalse((collection.settings_path(self.home).parent / 'code').exists())
        return interpreter, package, channel, active

    def test_first_install_hook_delivers_from_active_package_without_frozen_copy(self):
        interpreter, package, channel, active = self.installed_delivery_fixture()
        files = json.loads((package / 'verified.json').read_bytes())['manifest']['files']
        if files.keys() == set(channel.engine.adapter.files):
            # An unused file is allowed by the fixed-layout release verifier.
            (package / 'synthetic-unused.txt').touch()
        project = self.home / 'synthetic-project'
        project.mkdir()
        (project / 'Synthetic.uproject').touch()
        hook = package / 'src/agent_brain_client/interfaces/cli/collection.py'
        state = collection.settings_path(self.home).parent
        finished = self.home / 'synthetic-worker-finished'
        transcript = self.native / 'sessions/2026/01/02/rollout-2026-01-02T03-04-05-synthetic-session.jsonl'
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'synthetic-session'}}) + '\n')
        payload = dict(hook_event_name='UserPromptSubmit', cwd=str(project),
                       session_id='synthetic-session', turn_id='synthetic-turn',
                       prompt='Synthetic observation', transcript_path=str(transcript))

        def hook_and_wait(kind, expected):
            finished.unlink(missing_ok=True)
            result = subprocess.run([
                str(interpreter), '-I', '-B', str(hook), 'global-hook', '--harness', 'codex',
                '--device-home', str(self.home),
            ], input=json.dumps(dict(payload, hook_event_name=kind)).encode(),
               cwd=project, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            status = json.loads(result.stderr.splitlines()[0])
            self.assertEqual(status['status'], expected, status)
            if kind == 'UserPromptSubmit':
                self.assertEqual(status['local_queue']['state'], 'pending')
            self.assertNotIn('schedule_error_type', status)
            deadline = time.monotonic() + 8
            while not finished.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(finished.exists(), 'Detached worker did not finish')
            return metadata.private_json(state / 'delivery/status.json', cache=True)

        status = hook_and_wait('UserPromptSubmit', 'staged')
        outbox, = (state / 'outboxes').glob('*/outbox/*.json')
        row = metadata.private_json(outbox, cache=True)
        wire = self.home / 'synthetic-wire.jsonl'

        def submissions():
            lines = wire.read_text().splitlines() if wire.exists() else []
            return [request for request in map(json.loads, lines)
                    if request['method'] == 'memory_global_event_submit']

        sent = submissions()
        self.assertEqual((status['status'], row['state'], len(sent)), ('idle', 'confirmed', 1), status)
        self.assertEqual(finished.read_text(), '1')
        self.assertEqual(sent[0]['request_id'], row['request']['request_id'])
        self.assertEqual(sent[0]['params']['event'], row['request']['params']['event'])
        origin = outbox.parents[1] / 'origin.json'
        self.assertEqual(sent[0]['params']['origin'], metadata.private_json(origin, cache=True))
        self.assertEqual(hook_and_wait('SessionStart', 'session-ready')['status'], 'idle')
        self.assertEqual(submissions(), sent)
        self.assertEqual(channel.verified_active(serving=True), active)
        self.assertFalse((state / 'code').exists())

    def test_first_install_delivery_requires_exact_serving_release_and_keeps_frozen_entry(self):
        interpreter, package, channel, active = self.installed_delivery_fixture()
        settings = collection.settings_path(self.home)
        value = metadata.private_json(settings)

        def probe(root, flag, error=None):
            result = subprocess.run([
                str(interpreter), '-I', '-B', str(root / 'src/agent_brain_client/interfaces/cli/delivery.py'),
                '--device-home', str(self.home), flag,
            ], capture_output=True, text=True, timeout=15)
            if error is None:
                self.assertEqual(result.returncode, 0, result.stderr)
            else:
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)

        probe(package, '--probe-delivery')
        # The frozen entry requires its own complete inventory.
        probe(package, '--probe-frozen', 'Python runtime members outside reviewed layout')
        frozen = freeze_code(settings.parent, value['code_sha256'], source=package)
        probe(frozen, '--probe-frozen')
        probe(frozen, '--probe-delivery')

        other = package.with_name('0.8.24-' + 'b' * 40)
        shutil.copytree(package, other)
        probe(other, '--probe-delivery', 'Verified serving delivery package required')
        other_home = self.base / 'other-device' / package.relative_to(self.home)
        shutil.copytree(package, other_home)
        probe(other_home, '--probe-delivery', 'Verified serving delivery package required')

        # A healthy predecessor remains authoritative during candidate activation.
        metadata.write(channel.engine.journal, {'phase': 'activated', 'before': active})
        channel.engine.set_active(dict(active, commit='b' * 40, package=str(other)))
        probe(package, '--probe-delivery')
        probe(other, '--probe-delivery', 'Verified serving delivery package required')
        metadata.write(channel.engine.journal, {'phase': 'complete'})
        channel.engine.set_active(active)

        metadata.write(settings, dict(value, device_home=str(self.base)))
        probe(package, '--probe-delivery', 'Exact global memory settings required')
        hashes = dict(value['code_sha256'])
        hashes[next(iter(hashes))] = '0' * 64
        metadata.write(settings, dict(value, code_sha256=hashes))
        probe(package, '--probe-delivery')
        probe(frozen, '--probe-delivery', 'Refresh the Harness binding')
        metadata.write(settings, value)

        policy = self.home / '.config/agent-brain-client/first-install/release-policy.json'
        original = policy.read_bytes()
        metadata.write(policy, dict(channel.policy, origin='https://synthetic.invalid/other.git'))
        probe(package, '--probe-delivery', 'Metadata digest changed')
        policy.write_bytes(original)

        member = package / 'README.md'
        raw = member.read_bytes()
        member.write_bytes(raw + b'\nSYNTHETIC DRIFT\n')
        probe(package, '--probe-delivery', 'Active package integrity failed')
        member.write_bytes(raw)
        target = self.base / 'synthetic-readme'
        member.rename(target)
        member.symlink_to(target)
        probe(package, '--probe-delivery', 'Active package integrity failed')
        member.unlink()
        target.rename(member)
        probe(package, '--probe-delivery')

    def test_first_install_cli_emits_onboard_handoff_without_importing(self):
        self.fixture()
        original = wizard.run

        def run(home, native, connection, digest, *, harness):
            return original(home, native, connection, digest, harness=harness,
                            confirm=lambda *a: 'Continue', updater=self.update,
                            runner=self.runner, dependency_preparer=lambda *a, **k: self.materials,
                            store=self.store, transport=self.transport, secure_input=self.input,
                            installer=lambda p, d: content_install.apply(p, d, lambda value: None))

        output = io.StringIO()
        with patch.object(Path, 'home', return_value=self.home), \
                patch.dict(os.environ, CODEX_HOME=str(self.native)), \
                patch.object(sys, 'argv', ['agent_brain_client.runtime.installation.py', "--harness", "codex", '--connection', str(self.base / 'connection.json')]), \
                patch.object(wizard, 'run', side_effect=run), patch.object(sys, 'stdout', output):
            code = interfaces_cli_install.main()
        result = json.loads(output.getvalue())
        self.assertEqual(code, 2, result)
        self.assertEqual(result["outcome"], "partial", result)
        self.assertTrue(result['installed'])
        self.assertEqual(result['model_loaded'], 'unknown')
        self.assertEqual(result['onboard_check']['status'], 'coverage-check-required')
        self.assertIn('memory_onboard', result['onboard_check']['next_action'])
        self.assertFalse(result['onboard_check']['source_scan_performed'])
        self.assertFalse(result['onboard_check']['data_transferred'])


    def test_required_tools_and_helper_are_prepared_once_and_drift_is_preserved(self):
        self.fixture()
        builds = []

        def helper_builder(path):
            builds.append(path)
            path.write_bytes(b"SYNTHETIC_COMPILED_HELPER")
            path.chmod(0o700)
            return {
                "helper_sha256": adapters_storage_install_state.sha(path.read_bytes()),
                "source_sha256": self.materials["source_sha256"],
            }

        def runner(argv, **kwargs):
            if "--exec-path" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    self.materials["dependencies"]["git_exec_path"].encode(),
                    b"",
                )
            return self.runner(argv, **kwargs)

        with patch.object(
            runtime_dependencies.shutil, "which", return_value=self.materials["dependencies"]["git"]
        ):
            prepared = runtime_dependencies.prepare_dependencies(
                self.home, runner=runner, helper_builder=helper_builder
            )
            self.assertEqual(
                runtime_dependencies.prepare_dependencies(
                    self.home, runner=runner, helper_builder=helper_builder
                ),
                prepared,
            )
            self.assertEqual(len(builds), 1)
            builds[0].write_bytes(b"SYNTHETIC_DRIFT")
            with self.assertRaises(ValueError):
                runtime_dependencies.prepare_dependencies(
                    self.home, runner=runner, helper_builder=helper_builder
                )
        self.assertEqual(builds[0].read_bytes(), b"SYNTHETIC_DRIFT")
        self.assertEqual(self.prompts, 0)

    def test_first_install_prepared_environment_and_exact_native_transaction(
        self,
    ):
        self.fixture()
        result = self.run_wizard()
        self.assertTrue(result["installed"], result)
        state = self.home / ".config/agent-brain-client/first-install"
        ready = adapters_storage_private_files.private_json(next(state.glob("prior-ready-*.json")))
        self.assertEqual(ready["status"], "first-install-ready")
        self.assertFalse(ready["credential_accessed"])
        self.assertFalse(ready["native_changed"])
        plan = adapters_storage_private_files.private_json(state / "install-plan.json")
        self.assertIn(str(self.home / "tools"), plan["mcp"]["env"]["PATH"])
        self.assertIn('model="synthetic"', self.config.read_text())
        self.assertEqual(self.prompts, 1)
        self.assertEqual(sum(event[0] == "update" for event in self.events), 1)
        for path in state.rglob("*"):
            if path.is_file() and not path.is_symlink():
                self.assertNotIn(self.code.encode(), path.read_bytes())
                self.assertNotIn(
                    self.backend.record["credential"].encode(), path.read_bytes()
                )
        with patch.object(
            content_install,
            "inspect",
            return_value={"installed": True, "native_changed": False},
        ):
            self.assertTrue(self.run_wizard()["installed"])
        self.assertEqual(self.prompts, 1)

    def test_cancel_server_review_and_back_after_ready_never_prompts_or_pairs(self):
        self.fixture()
        before = self.config.read_bytes()
        self.assertEqual(
            self.run_wizard(confirm=lambda *args: "Cancel")["status"],
            "first-install-cancelled",
        )
        self.assertFalse(self.events)
        self.assertFalse(self.transport.prepares)
        decisions = iter(["Continue", "Back"])
        self.assertEqual(
            self.run_wizard(confirm=lambda *args: next(decisions))["status"],
            "first-install-ready",
        )
        self.assertEqual(self.config.read_bytes(), before)
        self.assertIsNone(self.backend.record)
        self.assertEqual(self.prompts, 0)

    def test_release_network_failure_never_declares_ready_or_requests_invitation(self):
        self.fixture()
        before = self.config.read_bytes()
        result = self.run_wizard(
            updater=lambda: {"status": "release-review-required", "active": None}
        )
        self.assertFalse(result["installed"])
        self.assertFalse(
            (self.home / ".config/agent-brain-client/first-install/ready.json").exists()
        )
        self.assertIsNone(self.backend.record)
        self.assertEqual(self.prompts, 0)
        self.assertEqual(self.config.read_bytes(), before)

    def test_lost_prepare_reply_restart_keeps_nonce_code_and_plan(self):
        self.fixture()
        self.transport.lost_prepare = True
        first = self.run_wizard()
        self.assertEqual(first["status"], "pairing-resume-required")
        saved = copy.deepcopy(self.backend.record)
        self.assertFalse(
            (self.home / ".config/agent-brain-client/first-install/ready.json").exists()
        )
        plan_path = (
            self.home / ".config/agent-brain-client/first-install/install-plan.json"
        )
        plan_before = plan_path.read_bytes()
        self.assertTrue(self.run_wizard()["installed"])
        self.assertEqual(plan_path.read_bytes(), plan_before)
        self.assertEqual(self.prompts, 1)
        self.assertEqual(self.transport.prepares, [(saved["code"], saved["nonce"])] * 2)
        self.assertEqual(sum(event[0] == "update" for event in self.events), 1)
        self.assertFalse(
            (self.home / ".config/agent-brain-client/first-install/ready.json").exists()
        )

    def test_native_drift_and_dependency_drift_stop_before_secure_input(self):
        self.fixture()
        decisions = iter(["Continue", "Back"])
        self.run_wizard(confirm=lambda *args: next(decisions))
        self.config.write_text('external_change="synthetic"\n')
        self.assertFalse(self.run_wizard()["installed"])
        self.assertEqual(self.prompts, 0)
        self.assertEqual(self.config.read_text(), 'external_change="synthetic"\n')
        Path(self.materials["dependencies"]["git"]).write_bytes(b"SYNTHETIC_DRIFT")
        self.assertFalse(self.run_wizard()["installed"])
        self.assertEqual(self.prompts, 0)
        self.assertIsNone(self.backend.record)

    def test_connection_and_existing_installation_preserved(self):
        self.fixture()
        registration = self.home / ".config/agent-brain-client/codex-mcp.json"
        registration.write_text("SYNTHETIC_EXISTING_REGISTRATION")
        registration.chmod(0o600)
        before = registration.read_bytes()
        self.assertFalse(self.run_wizard()["installed"])
        self.assertEqual(registration.read_bytes(), before)
        self.assertEqual(self.prompts, 0)

    def test_connection_rejects_credentials_scope_and_release_authority_drift(self):
        self.fixture()
        path = self.base / "bad-connection.json"
        for mutate in (
            lambda v: v.update(token="SYNTHETIC_TOKEN"),
            lambda v: v.update(endpoint="https://user:password@synthetic.invalid"),
            lambda v: v["release"].update(
                origin="https://synthetic.invalid/client.git"
            ),
            lambda v: v["release"].update(ref="refs/heads/releases/personal-stable"),
            lambda v: v["grant"].update(scopes=["read", "put", "remove"]),
        ):
            value = copy.deepcopy(self.connection)
            mutate(value)
            path.write_bytes(canonical(value))
            with self.assertRaises(ValueError):
                wizard.connection_file(path)

    def test_dependency_failure_never_inputs_invitation(self):
        self.fixture()

        def failed(*args, **kwargs):
            raise ValueError("SYNTHETIC_UNAVAILABLE_TOOLS")

        self.assertFalse(self.run_wizard(dependency_preparer=failed)["installed"])
        self.assertEqual(self.prompts, 0)
        self.assertFalse(self.transport.prepares)

    def test_old_readiness_is_preserved_but_invalidated_before_failed_recheck(self):
        self.fixture()
        decisions = iter(["Continue", "Back"])
        self.run_wizard(confirm=lambda *args: next(decisions))
        state = self.home / ".config/agent-brain-client/first-install"
        previous = (state / "ready.json").read_bytes()

        def failure(*a, **k):
            raise ValueError("SYNTHETIC_UNAVAILABLE_DEPENDENCY")

        self.assertFalse(self.run_wizard(dependency_preparer=failure)["installed"])
        self.assertFalse((state / "ready.json").exists())
        self.assertEqual(next(state.glob("prior-ready-*.json")).read_bytes(), previous)
        self.assertEqual(self.prompts, 0)

    def test_duplicate_launch_does_not_prompt_or_modify_transaction(self):
        self.fixture()
        lock = self.home / ".config/agent-brain-client/first-install/first-install.lock"
        with adapters_storage_install_state.private_lease(lock.parent):
            result = self.run_wizard(confirm=lambda *args: self.fail("Duplicate UI"))
        self.assertEqual(result["status"], "first-install-in-progress")
        self.assertEqual(self.prompts, 0)

    def test_interrupted_bootstrap_export_is_preserved_and_retry_uses_complete_package(
        self,
    ):
        self.fixture()
        actual = wizard.export

        def interrupted(source, output, **kwargs):
            output.mkdir()
            (output / "partial").write_bytes(b"SYNTHETIC_PARTIAL")
            raise OSError("SYNTHETIC_INTERRUPTION")

        with patch.object(wizard, "export", side_effect=interrupted):
            self.assertFalse(self.run_wizard()["installed"])
        state = self.home / ".config/agent-brain-client/first-install"
        evidence = next(state.glob("bootstrap-*/package/partial"))
        self.assertEqual(self.prompts, 0)
        with patch.object(wizard, "export", side_effect=actual):
            self.assertTrue(self.run_wizard()["installed"])
        self.assertEqual(evidence.read_bytes(), b"SYNTHETIC_PARTIAL")

    def test_native_protocol_probe_uses_pinned_dependencies_without_ambient_injection(
        self,
    ):
        self.fixture()
        decisions = iter(["Continue", "Back"])
        self.run_wizard(confirm=lambda *args: next(decisions))
        plan = adapters_storage_private_files.private_json(
            self.home / ".config/agent-brain-client/first-install/install-plan.json"
        )
        reply = {
            "status": "content-native-protocol-checked",
            "tool_contract_version": plan["tool_contract_version"],
            "protocol_checked": True,
            "authentication_checked": True,
            "native_changed": False,
            "tools": plan["mcp"]["enabled_tools"],
        }
        with patch.object(
            content_install.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, canonical(reply), b""),
        ) as process:
            content_install.probe(plan)
        environment = process.call_args.kwargs["env"]
        self.assertEqual(environment["PATH"], plan["mcp"]["env"]["PATH"])
        self.assertEqual(environment["HOME"], str(self.home))
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertNotIn("DYLD_INSERT_LIBRARIES", environment)

    def test_readiness_expiry_uses_one_clock_snapshot(self):
        self.fixture()
        decisions = iter(["Continue", "Back"])
        with patch.object(wizard.time, "time", side_effect=[2000, 2001]) as clock:
            self.assertEqual(
                self.run_wizard(confirm=lambda *args: next(decisions))["status"],
                "first-install-ready",
            )
        ready = adapters_storage_private_files.private_json(
            self.home / ".config/agent-brain-client/first-install/ready.json"
        )
        self.assertEqual(clock.call_count, 1)
        self.assertEqual(ready["valid_until"] - ready["prepared_at"], 600)

    def test_ready_receipts_reuse_installation_identity_across_restart_and_bind_drift(
        self,
    ):
        self.fixture()
        state = self.home / ".config/agent-brain-client/first-install"

        def ready_run():
            decisions = iter(["Continue", "Back"])
            return self.run_wizard(confirm=lambda *args: next(decisions))

        self.assertEqual(ready_run()["status"], "first-install-ready")
        first = adapters_storage_private_files.private_json(state / "ready.json")
        self.assertEqual(first["schema"], 2)
        self.assertEqual(ready_run()["status"], "first-install-ready")
        second = adapters_storage_private_files.private_json(state / "ready.json")
        self.assertEqual(first["installation_id"], second["installation_id"])
        identity = state / "installation-id.json"
        adapters_storage_private_files.save(identity, {"schema": 1, "id": "9" * 32})
        self.assertFalse(ready_run()["installed"])
        self.assertEqual(self.prompts, 0)

    def test_dependency_environment_is_optional_for_existing_policies(self):
        self.assertIsNone(dependency_environment({"runtime": {}}))


class PreparingReset(unittest.TestCase):
    setUp = fixtures.HTTPSContent.setUp
    coordinator = fixtures.HTTPSContent.coordinator
    input = fixtures.HTTPSContent.input

    def test_explicit_prepare_denial_can_be_cancelled_then_cleared_without_new_identity(
        self,
    ):
        def denied(*args):
            raise EnrollmentTerminal("pairing_prepare_rejected")

        self.transport.prepare = denied
        result = self.coordinator().run()
        self.assertEqual(result["reason"], "pairing_prepare_rejected")
        before = copy.deepcopy(self.backend.record)
        self.assertEqual(
            self.coordinator().reset_terminal(lambda *args: False)["status"],
            "pairing-reset-cancelled",
        )
        self.assertEqual(self.backend.record, before)
        self.assertEqual(
            self.coordinator().reset_terminal(lambda *args: True)["status"],
            "pairing-reenrollment-ready",
        )
        self.assertIsNone(self.backend.record)
        self.assertEqual(self.prompts, 1)

    def test_network_error_or_successful_prepare_cannot_clear_saved_recovery(self):
        self.transport.lost_prepare = True
        self.coordinator().run()
        before = copy.deepcopy(self.backend.record)
        self.transport.prepare = lambda *args: (_ for _ in ()).throw(
            OSError("SYNTHETIC_NETWORK_FAILURE")
        )
        self.assertEqual(
            self.coordinator().reset_terminal(
                lambda *args: self.fail("No reset UI on network failure")
            )["status"],
            "pairing-reset-blocked",
        )
        self.assertEqual(self.backend.record, before)
        self.transport.prepare = lambda *args: {"phase": "pending"}
        self.assertEqual(
            self.coordinator().reset_terminal(
                lambda *args: self.fail("No reset UI on success")
            )["status"],
            "pairing-reset-blocked",
        )
        self.assertEqual(self.backend.record, before)

    def test_bad_format_does_not_create_saved_preparing_record(self):
        coordinator = self.coordinator()
        coordinator.secure_input = lambda: "wrong"
        self.assertEqual(coordinator.run()["status"], "pairing-invalid-input")
        self.assertIsNone(self.backend.record)
        self.assertFalse(self.transport.prepares)

    def test_wire_prepare_denied_terminal_and_unknown_errors_retain_recovery(self):
        from urllib.error import HTTPError
        import io
        from agent_brain_client.adapters.server.https import PairingHTTPS

        transport = PairingHTTPS("https://synthetic.invalid")

        class Fail:
            code = "pairing_denied"
            status = 403

            def open(inner, request, **kwargs):
                raise HTTPError(
                    request.full_url,
                    inner.status,
                    "SYNTHETIC",
                    {},
                    io.BytesIO(
                        json.dumps(
                            {"error": {"code": inner.code, "debug": self.code}}
                        ).encode()
                    ),
                )

        opener = Fail()
        transport.opener = opener
        with self.assertRaises(EnrollmentTerminal) as caught:
            transport.prepare(self.code, "n" * 32, self.profile)
        self.assertEqual(caught.exception.reason, "pairing_prepare_rejected")
        for status, code in (
            (500, "pairing_denied"),
            (403, "unknown"),
            (401, "pairing_denied"),
        ):
            opener.status, opener.code = status, code
            with self.assertRaises(ValueError):
                transport.prepare(self.code, "n" * 32, self.profile)


class LifecycleExitCodes(unittest.TestCase):
    def test_update_reports_preserved_capture_separately_from_pending_setup(self):
        value = {"status": "client-update-current", "channel_checked": True}
        for status, code in (
            ("global-memory-source-paused", 0),
            ("global-memory-source-preserved", 0),
            ("global-memory-registration-pending", 2),
        ):
            with self.subTest(status=status):
                result = dict(value, source_setup={"status": status, "collection_enabled": False})
                self.assertEqual(interfaces_cli_install.lifecycle_exit("update", result), code)

    def test_unbind_dispatches_only_to_the_selected_host_port(self):
        from agent_brain_client.runtime import client_lifecycle

        home = Path("/synthetic/home").resolve()
        for harness, status, expected in (
            ("codebuddy", "host-unbound", 0),
            ("codex", "host-unbound", 0),
            ("codex", "host-unbind-shared-layout", 2),
        ):
            with (
                self.subTest(harness=harness, status=status),
                patch.object(Path, "home", return_value=home),
                patch.object(sys, "platform", "darwin"),
                patch("platform.machine", return_value="arm64"),
                patch.object(client_lifecycle, "unbind", return_value={"status": status}) as unbind,
                patch.object(client_lifecycle, "uninstall", side_effect=AssertionError("Host-only request")),
                patch.object(sys, "stdout", io.StringIO()) as output,
            ):
                self.assertEqual(interfaces_cli_install.main(["--action", "unbind", "--harness", harness]), expected)
                self.assertEqual(json.loads(output.getvalue())["status"], status)
                unbind.assert_called_once_with(home, harness=harness)

    def command(self,action,response):
        output=io.StringIO()
        with patch.object(sys,'argv',['agent_brain_client.runtime.installation.py', "--harness", "codex",'--action',action]),patch.object(sys,'platform','darwin'),patch('platform.machine',return_value='arm64'),patch.object(wizard.Path,'home',return_value=Path('/synthetic/home')),patch('agent_brain_client.runtime.client_lifecycle.'+action,return_value=response),patch.object(sys,'stdout',output):
            code=interfaces_cli_install.main()
        value = json.loads(output.getvalue())
        self.assertEqual(value.pop("operation"), action)
        self.assertEqual(value.pop("outcome"), "completed" if code == 0 else "review-required")
        if action == "status":
            self.assertEqual(value.pop("requested_harness"), "codex")
            value.pop("requested_harness_status")
            response.pop("requested_harness")
            response.pop("requested_harness_status")
        self.assertEqual(value, response)
        return code

    def test_uninstall_cancel_and_pending_are_not_success_even_if_installed(self):
        for status in ('uninstall-cancelled','uninstall-unconfirmed-writes','client-in-use'):
            with self.subTest(status=status):
                self.assertEqual(self.command('uninstall',{'status':status,'installed':True,'native_changed':False}),2)
        for status in ('client-uninstalled','client-not-installed'):
            self.assertEqual(self.command('uninstall',{'status':status,'installed':False,'native_changed':False}),0)

    def test_status_and_repair_do_not_confuse_review_with_absent_installation(self):
        self.assertEqual(self.command('status',{'status':'content-native-install-review-required','installed':False,'installation_present':True,'reason':'mcp-binding-changed','native_changed':False}),2)
        self.assertEqual(self.command('status',{'status':'content-native-client-installed','installed':True,'native_changed':False}),0)
        self.assertEqual(self.command('repair',{'status':'uninstall-recovery-review-required','installed':False,'native_changed':None}),2)
        paused={'status':'content-native-client-disabled','installed':False,'registration_verified':True,'native_changed':False}
        self.assertEqual(self.command('status',paused),0)
        self.assertEqual(self.command('uninstall',paused),2)
        self.assertEqual(self.command('status',{'status':'uninstall-state-review-required','installed':False,'native_changed':False,'uninstall_verified':False}),2)


class NewDeviceConnectionCLI(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin' and __import__('platform').machine().lower() == 'arm64', 'Actual Mac arm64 CLI only')
    def test_actual_process_requests_local_address_before_preparing_or_pairing(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            env = dict(os.environ, HOME=str(home), CODEX_HOME=str(home / 'codex'))
            result = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'src/agent_brain_client/interfaces/cli/install.py'), '--harness', 'codex'],
                                    env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2, result.stderr)
            value = json.loads(result.stdout)
            self.assertEqual(value['status'], 'access-input-cancelled')
            self.assertIn('HTTPS address', result.stderr)
            self.assertFalse(value['credential_accessed'])
            self.assertFalse((home / '.config').exists())

    def test_orphan_saved_description_is_not_a_trusted_identity(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            path = home / '.config/agent-brain-client/first-install/connection.json'
            adapters_storage_private_files.save(path, {'schema': 1})
            with patch.object(Path, 'home', return_value=home), \
                    patch.dict(os.environ, CODEX_HOME=str(home / 'native')), \
                    patch.object(sys, 'argv', ['agent_brain_client.runtime.installation.py', "--harness", "codex"]), \
                    patch.object(sys, 'stdout', io.StringIO()) as output, \
                    patch.object(wizard, 'run', side_effect=AssertionError('Must not install')):
                self.assertEqual(interfaces_cli_install.main(), 2)
            self.assertEqual(json.loads(output.getvalue())['status'], 'connection-recovery-review-required')


if __name__ == "__main__":
    unittest.main()
