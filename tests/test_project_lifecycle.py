"""Project-scoped identity uses the same foreground lifecycle, with synthetic ports."""
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch, call

from agent_brain_client.runtime import project_memory as project
from agent_brain_client.adapters.harnesses.codex import hooks
import test_content_outbox as outbox_fixture


class ProjectLifecycle(unittest.TestCase):
    def setUp(self):
        self.p = {'enabled': True, 'device_home': '/synthetic/home',
                  'project_root': '/synthetic/project', 'source_mode': 'codex-project',
                  'capture_policy': 'synthetic-capture', 'capture_policy_sha256': 'c' * 64,
                  'profile': 'synthetic-profile', 'profile_sha256': 'a' * 64,
                  'content_policy': 'synthetic-content', 'content_policy_sha256': 'b' * 64,
                  'recall_policy': 'synthetic-recall', 'recall_policy_sha256': 'd' * 64,
                  'query': 'default', 'max_items': 17, 'off_file': '/synthetic/off'}
        self.payload = {'hook_event_name': 'UserPromptSubmit', 'cwd': self.p['project_root'],
                        'session_id': 'session', 'turn_id': 'turn', 'prompt': 'Current question'}

    def hook(self, kind, *, stage, recall, schedule):
        payload = dict(self.payload, hook_event_name=kind)
        stream = SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))
        with (patch.object(project, 'active', return_value=True),
              patch.object(project.sys, 'stdin', stream),
              patch.object(project.sys, 'stderr', io.StringIO()),
              patch.object(project, 'capture', stage),
              patch.object(project, 'recalled', recall),
              patch.object(project, 'context', side_effect=lambda p, items: json.dumps(items) if items else ''),
              patch('agent_brain_client.runtime.delivery.schedule_project', schedule),
              patch.object(project, 'daily_command', side_effect=AssertionError('No foreground flush or sync'))):
            return project.hook_payload(self.p, settings_path='/synthetic/settings', settings_sha256='e' * 64)

    def test_session_start_only_schedules_and_prompt_recalls_current_question(self):
        stage = Mock(return_value={'status': 'staged', 'data_transferred': False})
        recall = Mock(return_value=[{'layer': 'L2', 'content': 'Synthetic project memory'}])
        schedule = Mock()
        self.assertEqual(self.hook('SessionStart', stage=stage, recall=recall, schedule=schedule), {})
        stage.assert_not_called()
        recall.assert_not_called()
        schedule.assert_called_once_with('/synthetic/home', '/synthetic/settings', 'e' * 64)
        order = []
        stage.side_effect = lambda *args: order.append('stage') or {'status': 'staged'}
        schedule.side_effect = lambda *args, **kwargs: order.append(
            'register' if kwargs.get('wake_worker') is False else 'schedule')
        recall.side_effect = lambda *args: order.append('recall') or [{'content': 'Synthetic'}]
        output = self.hook('UserPromptSubmit', stage=stage, recall=recall, schedule=schedule)
        self.assertEqual(order, ['register', 'stage', 'schedule', 'recall'])
        recall.assert_called_once_with(self.p, 'session', 'Current question')
        self.assertEqual(output['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')

    def test_stop_does_not_recall_and_still_schedules(self):
        stage, recall, schedule = Mock(return_value={'status': 'staged'}), Mock(), Mock()
        self.assertEqual(self.hook('Stop', stage=stage, recall=recall, schedule=schedule), {})
        stage.assert_called_once()
        recall.assert_not_called()
        self.assertEqual(schedule.call_args_list, [
            call('/synthetic/home', '/synthetic/settings', 'e' * 64, wake_worker=False),
            call('/synthetic/home', '/synthetic/settings', 'e' * 64),
        ])

    def test_project_recall_preserves_policy_identity_limit_and_all_layers(self):
        policy = {'schema': 1, 'project_id': 'project-bound', 'recall_sessions': ['session'],
                  'source_sessions': ['source-bound']}
        layers = {'L0': [], 'L1': [{'content': 'one'}], 'L2': [{'content': 'two'}], 'L3': {'content': 'three'}}
        client = Mock()
        client.request.return_value = {'result': {'recall': layers}}
        with (patch.object(project, 'private_json', side_effect=[{}, policy]),
              patch('agent_brain_client.adapters.server.https.HTTPSContentClient', return_value=client)):
            result = project.recalled(self.p, 'session', 'Precise current question')
        request = client.request.call_args.args[0]
        self.assertEqual(request['params']['query'], 'Precise current question')
        self.assertEqual(request['params']['limit'], 17)
        self.assertEqual(request['params']['source_sessions'], ['source-bound'])
        self.assertEqual(request['params']['context'], {'project_id': 'project-bound', 'session_id': 'session', 'cross_session': True})
        self.assertEqual([item['layer'] for item in result], ['L1', 'L2', 'L3'])
        self.assertEqual(self.p['query'], 'default')

    def test_each_observed_message_is_durable_without_transport_or_future_hook(self):
        fixture = outbox_fixture.Outbox('test_same_id_cannot_change_request_or_cas_base')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        queue = fixture.adapter
        p = dict(self.p, cache=str(queue.cache), codex_home=str(fixture.home),
                 off_file=str(fixture.home / 'memory.off'))
        scope = {'project_id': 'bound-project', 'session_id': 'bound-session'}
        with (patch('agent_brain_client.adapters.storage.pinned.pinned', return_value={}),
              patch.object(hooks, 'validate_hook_policy', return_value={'schema': 1}),
              patch.object(hooks, 'hook_enabled', return_value=True),
              patch('agent_brain_client.domain.content_policy.validate_policy'),
              patch('agent_brain_client.adapters.server.https.HTTPSContentClient', return_value=Mock()),
              patch('agent_brain_client.adapters.harnesses.codex.content.CodexContentSession', return_value=queue),
              patch.object(fixture.cloud, 'request', side_effect=AssertionError('Foreground network')) as transport):
            for kind, role in [('UserPromptSubmit', 'user'), ('Stop', 'assistant')]:
                with patch.object(hooks, 'native_hook_fields', return_value=(scope, 'turn', kind, 'Synthetic ' + role, 'key')):
                    result = project.capture(p, {})
                self.assertEqual(result['local_queue']['state'], 'pending')
                self.assertFalse(result['data_transferred'])
            transport.assert_not_called()
        rows = queue._queued()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['request']['params']['event']['messages'][0]['role'] for row in rows}, {'user', 'assistant'})
        self.assertTrue(all(row['request']['params']['event']['project_id'] == 'bound-project' for row in rows))

    def test_background_project_gate_rechecks_scope_and_disable(self):
        client, queue = Mock(), Mock()
        current = dict(self.p)
        policy = {'schema': 1, 'scopes': [{'cwd': self.p['project_root'], 'session_id': 'session', 'project_id': 'bound-project'}]}
        with (patch.object(project, 'load', side_effect=lambda *args: current),
              patch.object(project, 'active', side_effect=lambda p: p['enabled']),
              patch.object(project, 'adapter_for', return_value=(client, queue)),
              patch.object(project, 'private_json', return_value=policy),
              patch('agent_brain_client.adapters.storage.observed_migration.migrate_staged_turns'),
              patch.object(hooks, 'validate_hook_policy', side_effect=lambda p, **kwargs: p),
              patch.object(hooks, 'hook_enabled', return_value=True) as enabled):
            actual, allowed, allowed_event = project.delivery_port('/synthetic/settings', 'e' * 64)
            self.assertIs(actual, queue)
            self.assertTrue(allowed('session'))
            self.assertTrue(allowed_event({'session_id': 'session', 'project_id': 'bound-project'}))
            self.assertFalse(allowed_event({'session_id': 'session', 'project_id': 'other-project'}))
            enabled.return_value = False
            self.assertFalse(allowed('session'))
            enabled.return_value = True
            self.assertFalse(allowed('different-session'))
            current['enabled'] = False
            self.assertFalse(allowed('session'))
            current['enabled'] = True
            policy['scopes'][0]['cwd'] = '/other'
            self.assertFalse(allowed('session'))
        client.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
