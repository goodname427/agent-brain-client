"""Ordinary update of retained project Hooks over two isolated Client packages."""

import hashlib
import json
from pathlib import Path
import platform
import shlex
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_brain_client.adapters.harnesses.codebuddy import configuration as buddy
from agent_brain_client.adapters.packages import client as releases
from agent_brain_client.adapters.packages.frozen import freeze_code, runtime_pins
from agent_brain_client.adapters.packages.releases import UpdateBlocked
from agent_brain_client.adapters.platforms.filesystem import private_directory
from agent_brain_client.adapters.storage import collection, metadata, project_settings
from agent_brain_client.adapters.storage.registration import content_location, registration
from agent_brain_client.domain.scopes import digest as scope_digest
from agent_brain_client.runtime import project_memory, updates
import test_mac_first_install as fixtures


PROJECT_ENTRY = 'src/agent_brain_client/interfaces/cli/project.py'
PROJECT_SETTINGS = 'src/agent_brain_client/adapters/storage/project_settings.py'
HTTPS_ENTRY = 'src/agent_brain_client/adapters/server/https.py'
DELIVERY_ENTRY = 'src/agent_brain_client/runtime/delivery.py'

# The transport fixture returns a scoped recall row and records any request.
# Keep identity activation and queue construction in the actual package code.
SYNTHETIC_TRANSPORT = b'''
from io import BytesIO as _FixtureResponse
from types import SimpleNamespace as _FixtureNamespace

def native_store(profile, pin):
    from agent_brain_client.adapters.storage.metadata import private_json
    file = Path.home() / 'synthetic-enrollment.json'
    return _FixtureNamespace(load=lambda: private_json(file))

def _fixture_log(request):
    with (Path.home() / 'synthetic-project-wire.jsonl').open('a') as log:
        log.write(json.dumps(request) + '\\n')

class _FixtureGlobalPeer:
    def open(self, request, **kwargs):
        assert request.full_url == 'https://synthetic.invalid/v1/requests'
        body = json.loads(request.data)
        _fixture_log(body)
        return _FixtureResponse(json.dumps({'request_id': body['request_id'],
            'error': {'code': 'synthetic_global_recall_unavailable'}}).encode())

def RemoteClient(*args, **kwargs):
    return _FixtureNamespace(opener=_FixtureGlobalPeer())

def _fixture_request(self, request):
    _fixture_log(request)
    params = request['params']
    assert request['method'] == 'memory_query'
    self.recall_policy.check(request, self.device_id)
    assert params['source_scope'] == 'approved-project'
    return {'result': {'recall': {'L1': [{'content': 'Fixture project recall'}],
                                  'L2': [], 'L3': {'content': ''}}}}

HTTPSContentClient.request = _fixture_request
'''

OLD_INVENTORY_CHECK = b'''
_fixture_load = load

def load(path, sha, **kwargs):
    p = _fixture_load(path, sha, **kwargs)
    if p['code_sha256'] != runtime_pins(ROOT):
        raise ValueError('Original runtime must match project inventory')
    return p
'''


@unittest.skipUnless(sys.platform == 'darwin' and platform.machine() == 'arm64',
                     'Existing synthetic native enrollment fixture uses Mac arm64')
class LegacyProjectUpdate(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.FirstInstall('test_first_install_cli_emits_onboard_handoff_without_importing')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fixture_grant['ids'] = dict.fromkeys(fixture.fixture_grant['artifact_types'], 'personal-owner')
        fixture.code = fixture.authority.issue(fixture.fixture_grant, 300)
        fixture.fixture()
        fixture.runtime.stop()
        python = Path(sys.executable).resolve(strict=True)
        fixture.materials['dependencies'].update(
            python=str(python), python_sha256=hashlib.sha256(python.read_bytes()).hexdigest(),
        )
        self.assertTrue(fixture.run_wizard()['installed'])
        self.fixture = fixture
        self.home = fixture.home
        self.meta, self.policy = registration(self.home)
        metadata.write(self.home / 'synthetic-enrollment.json', fixture.backend.record)
        self.channel = releases.ClientReleases(self.meta['release_policy'], self.meta['release_policy_sha256'])
        files = releases.launcher_package_files(Path(self.meta['package']))
        payload = {name: (Path(self.meta['package']) / name).read_bytes() for name in files}
        payload[HTTPS_ENTRY] += SYNTHETIC_TRANSPORT
        # Preserve real task registration and outbox writes, and keep the
        # pending rows observable without starting a detached worker.
        payload[DELIVERY_ENTRY] += b'\n\ndef wake(home):\n    return False\n'
        old = dict(payload)
        old[PROJECT_SETTINGS] += OLD_INVENTORY_CHECK
        new = dict(payload)
        new['src/agent_brain_client/domain/synthetic_update_member.py'] = b'"""Synthetic new release member."""\n'
        self.old_release = self.release('a', '0.8.24', old)
        self.new_release = self.release('b', '0.8.25', new)
        self.select_release(self.old_release)
        self.channel.engine.pointer.unlink(missing_ok=True)
        self.channel.engine.journal.unlink(missing_ok=True)
        self.assertEqual(self.channel.update()['status'], 'updated')
        self.old_active = self.channel.verified_active(serving=True)
        self.old_package = Path(self.old_active['package'])
        self.new_package = self.channel.engine.local / 'packages' / ('0.8.25-' + 'b' * 40)
        self.assertEqual(old[PROJECT_ENTRY], new[PROJECT_ENTRY])
        self.root = private_directory(self.home / 'synthetic-project')
        self.child = private_directory(self.root / 'synthetic-child')
        self.neighbor = private_directory(self.home / 'synthetic-neighbor')
        for root in (self.root, self.neighbor):
            (root / 'Synthetic.uproject').touch()
        self.alias = self.home / 'synthetic-stable-entry'
        self.alias.symlink_to(self.old_package, target_is_directory=True)
        self.log = self.home / 'synthetic-project-wire.jsonl'

    def release(self, digit, version, payload):
        manifest = {
            'schema': 1, 'version': version, 'channel': self.channel.policy['channel'],
            'core_api': self.channel.policy['core_api'], 'state_schema': self.channel.policy['state_schema'],
            'python_min': '3.11', 'capabilities': self.channel.policy['capabilities'],
            'files': {name: metadata.digest(raw) for name, raw in payload.items()}, 'migrations': [],
        }
        return digit * 40, manifest, payload.__getitem__

    def select_release(self, release):
        self.channel.engine.source = SimpleNamespace(latest=lambda: release)

    def settings(self, harness):
        file = project_settings.project_settings(self.home, self.root, harness=harness)
        private_directory(file.parent)
        runtime = self.policy['runtime']
        native, cache = content_location(self.home, runtime, harness)
        off = file.parent / 'memory.off'
        capture, recall = file.parent / 'capture.json', file.parent / 'recall.json'
        project = 'synthetic-authorized-project'
        identity = {'profile_sha256': runtime['https_profile_sha256'],
                    'content_policy_sha256': runtime['content_policy_sha256']}
        capture_pin = metadata.write(capture, dict(
            identity, schema=2, enabled=True, lifecycle='until-disabled', source_mode='codex-project',
            notes_directory=None, off_file=str(off), scopes=[{'cwd': str(self.root),
                'project_id': project, 'session_id': None}],
        ))
        recall_pin = metadata.write(recall, {
            'schema': 2, 'enabled': True, 'lifecycle': 'until-disabled', 'source_mode': 'codex-project',
            'profile_sha256': runtime['https_profile_sha256'], 'endpoint': 'https://synthetic.invalid',
            'project_id': project, 'recall_session_id': 'synthetic-reader',
            'actor': self.fixture.backend.record['grant']['actor'],
            'device_id': self.fixture.backend.record['device_id'], 'max_results': 3,
            'source_session_id': 'synthetic-source', 'source_device_id': self.fixture.backend.record['device_id'],
            'off_file': str(off),
        })
        value = dict(identity, schema=1, harness=harness, enabled=True, project_root=str(self.root),
                     device_home=str(self.home), codex_home=str(native), profile=runtime['https_profile'],
                     content_policy=runtime['content_policy'], release_policy=self.meta['release_policy'],
                     release_policy_sha256=self.meta['release_policy_sha256'],
                     cache=str(cache if harness == 'codex' else self.home / '.config/agent-brain-client/codebuddy/project-outboxes' / metadata.digest(str(self.root).encode())),
                     off_file=str(off), query='', max_items=3, max_context_bytes=12288,
                     code_sha256=runtime_pins(self.old_package), source_mode='codex-project',
                     capture_policy=str(capture), capture_policy_sha256=capture_pin,
                     recall_policy=str(recall), recall_policy_sha256=recall_pin)
        if harness == 'codex':  # Also exercise the pre-Harness schema-1 record.
            value.pop('harness')
        self.file, self.value = file, value
        self.pin = metadata.write(file, value)
        return project

    def install_project_hook(self, harness, entry):
        if harness == 'codex':
            from agent_brain_client.adapters.harnesses.codex import project_configuration as writer
            attribute, target = 'ENTRY', entry / PROJECT_ENTRY
        else:
            from agent_brain_client.adapters.harnesses.codebuddy import project_configuration as writer
            attribute, target = 'ROOT', entry
        with patch.object(writer, attribute, target):
            project_memory.install(dict(self.value, harness=harness), self.file, self.pin)

    def install(self, harness, entry):
        self.install_project_hook(harness, entry)
        state = collection.settings_path(self.home)
        private_directory(state.parent)
        scope = {'schema': 1, 'profile_id': 'a' * 32, 'include_roots': [], 'exclude_roots': [],
                 'non_project_roots': [], 'all_projects': True, 'recall_personal': True}
        pin = scope_digest(scope)
        value = {'schema': 1, 'device_home': str(self.home), 'enabled': True, 'scope': scope,
                 'approved_scope_sha256': pin, 'server_scope_sha256': pin,
                 'legacy_projects': [], 'code_sha256': self.value['code_sha256'],
                 'operator': None, 'operator_sha256': None}
        metadata.write(state, value)
        if harness == 'codex':
            code = """
import json, sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1] + '/src')
from agent_brain_client.adapters.harnesses.codex.collection_installation import install
print(json.dumps(install(SimpleNamespace(device_home=sys.argv[2], profile_id=None,
                                         operator=None, operator_sha256=None))))
"""
            run = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(self.old_package),
                                  str(self.home)], text=True, capture_output=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads(run.stdout)
            self.assertEqual(result['legacy_project_roots'], [str(self.root)])
        else:
            freeze_code(state.parent, value['code_sha256'], source=self.old_package)
            buddy.install(self.home, self.old_package, None, surface='cli')
        self.global_settings = state
        self.global_before = metadata.private_json(state)

    def command(self, harness, *, project, event):
        if project:
            target = self.root / ('.codex/hooks.json' if harness == 'codex' else '.codebuddy/settings.local.json')
        elif harness == 'codex':
            target = self.fixture.native / 'config.toml'
        else:
            target = self.home / '.codebuddy/settings.json'
        if target.suffix == '.toml':
            import tomllib
            value = tomllib.loads(target.read_text())
        else:
            value = json.loads(target.read_text())
        return value['hooks'][event][0]['hooks'][0]['command']

    def hook(self, harness, cwd, *, project, turn, event='UserPromptSubmit'):
        command = self.command(harness, project=project, event=event)
        payload = {'cwd': str(cwd), 'hook_event_name': event, 'session_id': 'synthetic-session',
                   'turn_id': turn, 'generation_id': turn, 'prompt': 'What is the fixture project plan?'}
        run = subprocess.run(shlex.split(command), cwd=cwd, input=json.dumps(payload),
                             text=True, capture_output=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout), run.stderr

    def update(self):
        self.select_release(self.new_release)
        with patch('agent_brain_client.runtime.installation_completion.source_setup', return_value={}):
            return updates.manage_update(self.home, 'update', channel_factory=lambda *args: self.channel)

    def point_entry(self, package):
        self.alias.unlink()
        self.alias.symlink_to(package, target_is_directory=True)

    def assert_project_turn(self, harness, project, turn):
        before = self.log.read_text().splitlines() if self.log.exists() else []
        output, diagnostics = self.hook(harness, self.root, project=True, turn=turn)
        self.assertIn('Fixture project recall', output.get('hookSpecificOutput', {}).get('additionalContext', ''), diagnostics)
        global_output, global_diagnostics = self.hook(harness, self.root, project=False, turn=turn)
        self.assertEqual(global_output, {}, global_diagnostics)
        requests = self.log.read_text().splitlines()[len(before):]
        self.assertEqual(len(requests), 1, global_diagnostics)
        self.assertEqual(json.loads(requests[0])['params']['context']['project_id'], project)
        self.assertIn('"status": "staged"', diagnostics)
        if harness == 'codebuddy':
            self.assertIn('"status": "project-source-delegated"', global_diagnostics)

    def test_codex_ordinary_update_keeps_project_and_global_owners(self):
        self.exercise_update('codex')

    def test_codebuddy_ordinary_update_keeps_project_and_global_owners(self):
        self.exercise_update('codebuddy')

    def assert_no_project_access(self, harness, turn):
        wire = self.log.read_bytes()
        cache = Path(self.value['cache'])
        rows = {p: p.read_bytes() for p in (cache / 'outbox').glob('*.json')}
        for project in (True, False):
            output, diagnostics = self.hook(harness, self.root, project=project, turn=turn)
            self.assertEqual(output, {}, diagnostics)
            self.assertNotIn('"status": "staged"', diagnostics)
        self.assertEqual(self.log.read_bytes(), wire)
        self.assertEqual({p: p.read_bytes() for p in (cache / 'outbox').glob('*.json')}, rows)

    def assert_denied_projects(self, harness):
        marker = Path(self.value['off_file'])
        metadata.write(marker, {'synthetic': 'stopped'})
        self.assert_no_project_access(harness, 'synthetic-stopped-turn')
        self.assertEqual(metadata.private_json(marker), {'synthetic': 'stopped'})
        marker.unlink()

        # Corrupt an independently pinned policy while keeping native command
        # and settings intact. No permission can be inferred from a root alone.
        capture = Path(self.value['capture_policy'])
        capture_before = metadata.private_json(capture)
        metadata.write(capture, dict(capture_before, enabled=False))
        self.assert_no_project_access(harness, 'synthetic-unverified-policy-turn')
        self.assertEqual(metadata.write(capture, capture_before), self.value['capture_policy_sha256'])

        # Install the command for a real legacy finite record whose window
        # already expired. The global route stays reserved on both hosts.
        old = metadata.private_json(self.value['recall_policy'])
        expired = {key: old[key] for key in ('actor', 'device_id', 'endpoint',
                                             'profile_sha256', 'project_id', 'max_results', 'off_file')}
        expired.update(schema=1, enabled=True, not_before=1, expires_at=60,
                       source_sessions=['synthetic-source'], recall_sessions=['synthetic-reader'])
        file = self.file.parent / 'expired-recall.json'
        self.value = dict(self.value, recall_policy=str(file), recall_policy_sha256=metadata.write(file, expired))
        self.pin = metadata.write(self.file, self.value)
        self.install_project_hook(harness, self.alias)
        self.assert_no_project_access(harness, 'synthetic-expired-turn')

    def exercise_update(self, harness):
        project = self.settings(harness)
        self.install(harness, self.alias)
        self.assert_project_turn(harness, project, 'synthetic-old-turn')
        cache = Path(self.value['cache'])
        outbox, = (cache / 'outbox').glob('*.json')
        original = outbox.read_bytes()
        self.assertEqual(metadata.private_json(outbox, cache=True)['state'], 'pending')
        old_command = self.command(harness, project=True, event='UserPromptSubmit')
        old_settings = self.file.read_bytes()
        report = self.update()
        self.assertEqual(report['status'], 'client-update-updated', report)
        self.assertEqual(Path(self.channel.verified_active(serving=True)['package']), self.new_package)
        self.assertEqual(self.command(harness, project=True, event='UserPromptSubmit'), old_command)
        # The still-independent old executable keeps legacy settings working.
        self.assert_project_turn(harness, project, 'synthetic-independent-turn')
        self.point_entry(self.new_package)
        self.assert_project_turn(harness, project, 'synthetic-new-turn')
        self.assertEqual(len(list((cache / 'outbox').glob('*.json'))), 3)
        self.assertEqual(outbox.read_bytes(), original)
        # Native retries keep the same project operation and do not create a
        # second queue row or move it to the personal capture owner.
        self.hook(harness, self.root, project=True, turn='synthetic-new-turn')
        self.assertEqual(len(list((cache / 'outbox').glob('*.json'))), 3)
        self.assertEqual(self.file.read_bytes(), old_settings)
        self.assertEqual(self.update()['status'], 'client-update-current')
        self.assertEqual(self.command(harness, project=True, event='UserPromptSubmit'), old_command)
        self.assertEqual({k: v for k, v in metadata.private_json(self.global_settings).items() if k != 'code_sha256'},
                         {k: v for k, v in self.global_before.items() if k != 'code_sha256'})

        # Same bounded project grant applies only to its exact cwd. Ordinary
        # origins continue through the already approved global source path.
        for cwd in (self.child, self.neighbor):
            output, diagnostic = self.hook(harness, cwd, project=True, turn='synthetic-outside')
            self.assertEqual(output, {}, diagnostic)
            _, diagnostic = self.hook(harness, cwd, project=False, turn='synthetic-global-' + cwd.name)
            self.assertIn('"status": "staged"', diagnostic)
        global_cache = self.home / '.config/agent-brain-client' / ('global-memory' if harness == 'codex' else harness) / 'outboxes'
        global_origins = {metadata.private_json(path, cache=True)['cwd'] for path in global_cache.glob('*/origin.json')}
        self.assertEqual(global_origins, {str(self.child), str(self.neighbor)})
        self.assert_denied_projects(harness)
        self.assertEqual(outbox.read_bytes(), original)

    def test_interrupted_candidate_and_health_rollback_keep_project_entry_and_queue_paired(self):
        harness = 'codex'
        project = self.settings(harness)
        self.install(harness, self.alias)
        self.assert_project_turn(harness, project, 'synthetic-before-interruption')
        outbox, = (Path(self.value['cache']) / 'outbox').glob('*.json')
        original = outbox.read_bytes()
        settings = self.file.read_bytes()
        global_settings = self.global_settings.read_bytes()
        command = self.command(harness, project=True, event='UserPromptSubmit')

        self.select_release(self.new_release)
        with patch.object(self.channel.engine.adapter, 'health', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.channel.update()
        self.assertEqual(self.channel.verified_active(serving=True), self.old_active)
        self.point_entry(self.new_package)
        output, diagnostics = self.hook(harness, self.root, project=True, turn='synthetic-candidate')
        self.assertEqual(output, {}, diagnostics)
        self.assertIn('"status":"review-required"', diagnostics)
        self.assert_no_project_access(harness, 'synthetic-candidate')

        with patch.object(self.channel.engine.adapter, 'health', side_effect=UpdateBlocked('Synthetic failed health check')):
            result = self.update()
        self.assertEqual(result['active_package_commit'], self.old_active['commit'], result)
        self.assertNotEqual(result['status'], 'client-update-updated', result)
        self.assertEqual(self.channel.verified_active(serving=True), self.old_active)
        self.assertEqual(self.global_settings.read_bytes(), global_settings)
        self.assert_no_project_access(harness, 'synthetic-rejected-candidate')
        self.point_entry(self.old_package)
        self.assert_project_turn(harness, project, 'synthetic-rolled-back')
        self.assertEqual(self.update()['status'], 'client-update-updated')
        self.point_entry(self.new_package)
        self.assert_project_turn(harness, project, 'synthetic-after-retry')
        self.assertEqual(self.file.read_bytes(), settings)
        self.assertEqual(self.command(harness, project=True, event='UserPromptSubmit'), command)
        self.assertEqual(outbox.read_bytes(), original)

        new_member = self.new_package / 'src/agent_brain_client/domain/synthetic_update_member.py'
        new_member.write_bytes(b'# Synthetic changed installed bytes\n')
        self.assert_no_project_access(harness, 'synthetic-tampered-package')
