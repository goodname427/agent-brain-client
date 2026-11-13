import agent_brain_client.interfaces.cli.collection as collection_cli
import agent_brain_client.runtime.updates as runtime_updates
import agent_brain_client.adapters.storage.collection as source_policy
import agent_brain_client.adapters.storage.metadata as metadata
"""Source activation/control regressions with synthetic state and no network."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import agent_brain_client.runtime.collection as gm
import agent_brain_client.runtime.source_enrollment as operator
from agent_brain_client.domain.scopes import digest


class GlobalMemoryActivation(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='synthetic-source-activation-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.file = source_policy.settings_path(self.home)
        self.file.parent.mkdir(parents=True, mode=0o700)
        self.scope = {'schema':1, 'profile_id':'a' * 32,
                      'include_roots':[{'root':str(self.home), 'sharing':'personal'}],
                      'exclude_roots':[], 'non_project_roots':[], 'recall_personal':True}
        self.value = {'device_home':str(self.home), 'enabled':True, 'scope':self.scope,
                      'approved_scope_sha256':digest(self.scope),
                      'server_scope_sha256':digest(self.scope), 'legacy_projects':[], 'operator':None}
        self.off = self.file.parent / 'global.off'

    def test_empty_source_resume_cannot_claim_activation_or_access_credentials(self):
        value = copy.deepcopy(self.value)
        value.update(enabled=False, approved_scope_sha256=None, server_scope_sha256=None)
        value['scope']['include_roots'] = []
        self.off.touch()
        before = self.off.read_bytes()
        with patch.object(gm, 'CollectionClient', side_effect=AssertionError('No credentials')):
            result = gm.resume(value)
        self.assertEqual(result['status'], 'global-memory-activation-required')
        self.assertFalse(result['remote_confirmed'])
        self.assertFalse(result['collection_enabled'])
        self.assertEqual(self.off.read_bytes(), before)
        self.assertFalse(self.file.exists())

    def test_registration_retries_exact_approved_metadata_without_new_identity(self):
        value = dict(self.value, enabled=False, server_scope_sha256=None)
        packet = {'schema':2, 'scope':self.scope, 'approved_scope_sha256':digest(self.scope),
                  'client':{'source_device_id':'c'*32}}
        response = {'result':{'source_registered':True,'source_device_id':'c'*32,
                             'global_profile_id':self.scope['profile_id'],'scope_sha256':digest(self.scope)}}
        client = SimpleNamespace(harness="codex", device_id='c'*32, wire=Mock(side_effect=[
            {'error':{'code':'transport_unconfirmed','retryable':True}}, response]))
        before = copy.deepcopy(value)
        with (patch('agent_brain_client.adapters.server.collection.CollectionClient',return_value=client),
              patch.object(operator,'activate',return_value={'activated':True}) as activate):
            first = operator.register(value,packet)
            self.assertFalse(first['activated']); activate.assert_not_called()
            self.assertEqual(value,before)
            self.assertTrue(operator.register(value,packet)['activated'])
        self.assertEqual(client.wire.call_args_list[0],client.wire.call_args_list[1])
        self.assertEqual(client.wire.call_args.args[2],'source_'+digest(packet))
        activate.assert_called_once_with(value,self.scope)

    def test_wrong_packet_cannot_register_or_resume(self):
        value = dict(self.value, enabled=False, server_scope_sha256=None)
        packet = {'schema':2,'scope':self.scope,'approved_scope_sha256':'0'*64,
                  'client':{'source_device_id':'c'*32}}
        with patch('agent_brain_client.adapters.server.collection.CollectionClient',side_effect=AssertionError('No credential access')):
            with self.assertRaises(ValueError): operator.register(value,packet)

    def test_each_missing_approval_keeps_source_fenced_without_server_request(self):
        for field, missing in (('enabled', False), ('approved_scope_sha256', None),
                               ('server_scope_sha256', None)):
            with self.subTest(field=field):
                value = dict(self.value, **{field:missing})
                self.off.touch()
                with patch.object(gm, 'CollectionClient', side_effect=AssertionError('No credentials')):
                    self.assertFalse(gm.resume(value)['remote_confirmed'])
                self.assertTrue(self.off.exists())

    def test_failed_or_nonconfirming_resume_retains_fence_and_exact_denial(self):
        error = {'code':'global_source_device_denied', 'retryable':False}
        responses = [{'error':error}, {'result':{}},
                     {'result':{'schema':1, 'stopped':True}},
                     {'result':{'schema':1, 'stopped':0}}]
        for response in responses:
            with self.subTest(response=response):
                client = SimpleNamespace(harness="codex", wire=Mock(return_value=response))
                with patch.object(gm, 'CollectionClient', return_value=client):
                    result = gm.resume(self.value)
                self.assertEqual(result['status'], 'global-memory-resume-unconfirmed')
                self.assertFalse(result['collection_enabled'])
                self.assertEqual(result['server_error'], response.get('error'))
                self.assertTrue(self.off.exists())
                client.wire.assert_called_once_with('memory_global_resume', {})
                self.assertFalse(self.file.exists())

    def test_transport_exception_leaves_even_previously_running_source_fenced(self):
        client = SimpleNamespace(harness="codex", wire=Mock(side_effect=OSError('Synthetic transport failure')))
        with patch.object(gm, 'CollectionClient', return_value=client), self.assertRaises(OSError):
            gm.resume(self.value)
        self.assertTrue(self.off.exists())

    def test_confirmed_resume_only_clears_local_fence_without_changing_scope(self):
        before = copy.deepcopy(self.value)
        self.off.touch()
        client = SimpleNamespace(harness="codex", wire=Mock(return_value={'result':{'schema':1, 'stopped':False}}))
        with patch.object(gm, 'CollectionClient', return_value=client): result = gm.resume(self.value)
        self.assertTrue(result['remote_confirmed'])
        self.assertTrue(result['collection_enabled'])
        self.assertFalse(self.off.exists())
        self.assertEqual(self.value, before)
        self.assertFalse(self.file.exists())

    def test_local_stop_guidance_resumes_original_approval_without_recreating_packet(self):
        self.off.touch()
        self.assertEqual(source_policy.admission_state(self.value)['next_action'],
                         'explicit-global-resume-for-the-existing-approved-source')
        pending = dict(self.value, server_scope_sha256=None)
        self.assertEqual(source_policy.admission_state(pending)['next_action'],
                         'check-server-and-have-existing-operator-apply-the-saved-device-packet')

    def test_legacy_resume_keeps_original_project_control_and_no_expanded_source_claim(self):
        value = dict(self.value, enabled=False, approved_scope_sha256=None,
                     server_scope_sha256=None,
                     legacy_projects=[{'root':'synthetic-project', 'settings':'synthetic-settings', 'settings_sha256':'b' * 64}])
        source = {'off_file':str(self.home / 'synthetic-project.off')}
        with (patch.object(gm, 'CollectionClient', side_effect=AssertionError('No global credentials')),
              patch('agent_brain_client.adapters.storage.project_settings.load', return_value=source),
              patch('agent_brain_client.runtime.project_memory.daily_command', return_value={}) as command):
            result = gm.resume(value)
        command.assert_called_once_with(source, 'resume')
        self.assertEqual(result['status'], 'global-memory-legacy-resumed')
        self.assertTrue(result['legacy_capture_resumed'])
        self.assertFalse(result['collection_enabled'])

    def test_cli_pending_resume_returns_nonzero_and_actionable_json(self):
        value = dict(self.value, enabled=False)
        output = io.StringIO()
        with (patch.object(gm, 'load', return_value=value),
              patch.object(sys, 'argv', ['global_memory', 'global-resume']),
              patch.object(sys, 'stdout', output),
              patch.object(gm, 'CollectionClient', side_effect=AssertionError('No credentials'))):
            self.assertEqual(collection_cli.main(), 2)
        self.assertEqual(json.loads(output.getvalue())['status'], 'global-memory-activation-required')

    def test_migrated_legacy_sources_are_not_reenabled_while_global_admission_is_pending(self):
        value = dict(self.value, enabled=False, server_scope_sha256=None,
                     legacy_projects=[{'capture_delegated':False}])
        with (patch.object(gm, 'CollectionClient', side_effect=AssertionError('No credentials')),
              patch('agent_brain_client.runtime.project_memory.daily_command', side_effect=AssertionError('No legacy reactivation'))):
            result = gm.resume(value)
        self.assertEqual(result['status'], 'global-memory-activation-required')

    def test_cli_denied_activation_returns_nonzero_with_original_error(self):
        output = io.StringIO()
        result = {'status':'global-memory-admission-unconfirmed', 'activated':False,
                  'server_error':{'code':'global_source_device_denied', 'retryable':False}}
        with (patch.object(operator, 'configure', return_value=result),
              patch.object(sys, 'argv', ['global_memory', 'global-configure', '--activate-packet', 'synthetic-packet']),
              patch.object(sys, 'stdout', output)):
            self.assertEqual(collection_cli.main(), 2)
        self.assertEqual(json.loads(output.getvalue()), result)

    def test_activation_does_not_clear_fence_on_missing_resume_confirmation(self):
        value = dict(self.value, enabled=False, server_scope_sha256=None)
        status = {'result':{'source_registered':True, 'scope_sha256':digest(self.scope),
                            'global_profile_id':self.scope['profile_id'], 'source_device_id':'c' * 32}}
        client = SimpleNamespace(harness="codex", device_id='c' * 32, wire=Mock(side_effect=[status, {'result':{}}]))
        with patch('agent_brain_client.adapters.server.collection.CollectionClient', return_value=client):
            result = operator.activate(value, self.scope)
        self.assertFalse(result['activated'])
        self.assertFalse(result['collection_enabled'])
        self.assertFalse(self.off.exists())
        self.assertEqual(value, dict(self.value, enabled=False, server_scope_sha256=None))

    def test_configuration_connection_failure_keeps_current_source_active(self):
        before = copy.deepcopy(self.value)
        args = SimpleNamespace(harness="codex", device_home=str(self.home), include_root=[], isolate_root=[],
            exclude_root=[], non_project_root=[], all_projects=True, activate_packet=None)
        client = SimpleNamespace(harness="codex", device_id='c'*32, wire=Mock(return_value={'error':{'code':'transport_unconfirmed'}}))
        with patch('agent_brain_client.adapters.storage.collection.load', return_value=self.value), patch('agent_brain_client.adapters.server.collection.CollectionClient', return_value=client):
            result = operator.configure(args)
        self.assertEqual(result['status'], 'global-memory-connection-pending')
        self.assertEqual(self.value, before)
        self.assertFalse(self.off.exists())
        self.assertFalse(self.file.exists())
        client.wire.assert_called_once_with('memory_global_status', {})


class AutomaticCapture(unittest.TestCase):
    setUp = GlobalMemoryActivation.setUp

    def test_automatic_setup_binds_failed_placeholder_and_retries_lost_resume(self):
        from agent_brain_client.runtime.installation_completion import source_setup
        from agent_brain_client.domain.scopes import select
        value = dict(self.value, enabled=False, server_scope_sha256=None)
        metadata.write(self.file, value)
        profile = self.file.parent/'synthetic-profile.json'
        metadata.write(profile, {'endpoint':'https://synthetic.invalid',
            'grant':{'actor':'synthetic-owner','platform':'win32-amd64','scopes':['read','put']}})
        runtime = {'https_profile':str(profile),'https_profile_sha256':metadata.digest(profile.read_bytes()),
            'content_policy_sha256':'f'*64}
        state = {'scope':None,'fail_resume':True,'calls':[]}
        def wire(method, params, rid=None):
            state['calls'].append(method)
            scope = state['scope']
            if method == 'memory_global_status':
                return {'result':{'metadata_only':True,'source_registered':scope is not None,
                    'source_device_id':'c'*32,'global_profile_id':'b'*32,
                    'scope_sha256':digest(scope) if scope else None}}
            if method == 'memory_global_register':
                self.assertEqual(params['scope']['profile_id'], 'b'*32)
                state['scope'] = copy.deepcopy(params['scope'])
                return {'result':{'source_registered':True,'source_device_id':'c'*32,
                    'global_profile_id':'b'*32,'scope_sha256':digest(state['scope'])}}
            if method == 'memory_global_resume':
                return {'error':{'code':'transport_unconfirmed'}} if state['fail_resume'] else {'result':{'schema':1,'stopped':False}}
            self.fail('No model or history request: ' + method)
        client = SimpleNamespace(harness="codex", device_id='c'*32, wire=wire)
        def load(*args, **kwargs): return metadata.private_json(self.file)
        with (patch('agent_brain_client.adapters.storage.collection.load', side_effect=load),
              patch('agent_brain_client.adapters.server.collection.CollectionClient', return_value=client),
              patch('agent_brain_client.adapters.storage.registration.registration', return_value=(None, {'runtime':runtime})),
              patch.object(gm, 'discover', side_effect=AssertionError('No history scan'))):
            before = self.file.read_bytes()
            self.assertFalse(source_setup(self.home, choose=lambda _:self.fail('No approval prompt'))['collection_enabled'])
            self.assertEqual(self.file.read_bytes(), before)
            state['fail_resume'] = False
            self.assertTrue(source_setup(self.home)['collection_enabled'])
        self.assertEqual(state['calls'].count('memory_global_register'), 1)
        stored = load()
        self.assertTrue(stored['scope']['all_projects'])
        self.assertTrue(source_policy.approved(stored))
        for root in ('C:/synthetic/new-project', 'Z:/future-volume/project', '/synthetic/elsewhere'):
            local = {'kind':'project','cwd':root,'project_root':root,'repository_root':root}
            self.assertEqual(select(stored['scope'], local)['sharing'], 'personal')
        self.assertIsNone(select(stored['scope'], {'kind':'non-project','cwd':str(self.home),
            'project_root':None,'repository_root':None}))

    def test_noninteractive_update_runs_source_setup_and_status_does_not(self):
        from agent_brain_client.application.installation.update import ClientUpdate
        import agent_brain_client.runtime.installation as wizard
        import agent_brain_client.runtime.installation_completion as guide
        result = {'status':'client-update-current','channel_checked':True}
        with (patch.object(ClientUpdate, '_run', return_value=result),
              patch.object(guide, 'source_setup', return_value={'collection_enabled':True}) as setup):
            metadata.write(self.file, self.value)
            self.assertTrue(runtime_updates.manage_update(self.home, 'update')['source_setup']['collection_enabled'])
            setup.assert_called_once_with(self.home)
            setup.reset_mock()
            runtime_updates.manage_update(self.home, 'check-update')
            setup.assert_not_called()


if __name__ == '__main__': unittest.main()
