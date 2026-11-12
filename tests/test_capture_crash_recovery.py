"""Fault injection over synthetic homes, identities, queues and stdout only."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_content_outbox as fixture
from test_event_hook import accepted
from agent_brain_client.application.memory.observations import stage_messages, resume_staging
from agent_brain_client.application.queue.delivery import deliver_pending
from agent_brain_client.adapters.harnesses.codex import collection, hooks
from agent_brain_client.adapters.harnesses.codex.content import CodexContentSession
from agent_brain_client.adapters.storage import metadata
from agent_brain_client.domain import contract
from agent_brain_client.domain.errors import CacheBlocked
from agent_brain_client.domain.scopes import digest
from agent_brain_client.runtime import delivery, project_memory


class CaptureRecovery(unittest.TestCase):
    def setUp(self):
        self.f = fixture.Outbox()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.queue, self.home = self.f.adapter, self.f.home
        root = str(self.home)
        self.local = {'cwd': root, 'kind': 'project', 'project_root': root, 'repository_root': root}
        self.value = {'device_home': root, 'scope': {
            'schema': 1, 'profile_id': 'a' * 32, 'all_projects': True,
            'include_roots': [], 'exclude_roots': [], 'non_project_roots': [], 'recall_personal': True}}
        runtime = {'https_profile': 'synthetic-profile', 'https_profile_sha256': 'a' * 64,
                   'content_policy_sha256': 'b' * 64}
        self.services = SimpleNamespace(settings_path=lambda _: self.home / 'settings.json',
                                       adapter=lambda *_: (None, self.queue))
        for target in [
            patch.object(collection.registration_store, 'registration', return_value=({}, {'runtime': runtime})),
            patch.object(collection.metadata_io, 'private_json', wraps=metadata.private_json),
        ]:
            target.start()
            self.addCleanup(target.stop)
        # Only the synthetic profile lookup is intercepted; real private queue
        # files continue through their normal validation.
        original = metadata.private_json._mock_wraps
        metadata.private_json.side_effect = lambda p, *a, **kw: (
            {'grant': {'actor': 'synthetic-owner'}} if p == 'synthetic-profile' else original(p, *a, **kw))

    def stage(self, text='Synthetic observation', operation='synthetic-turn'):
        return stage_messages(self.queue, project_id='synthetic-project',
            session_id='synthetic-session', operation_id=operation, phase='prompt',
            messages=[{'role': 'user', 'content': text}])

    def payload(self, kind, text, turn='synthetic-turn'):
        return {'hook_event_name': kind, 'session_id': 'synthetic-session', 'turn_id': turn,
                'cwd': str(self.home), 'prompt': text, 'last_assistant_message': text,
                'stop_hook_active': False}

    def captured(self, request_ids=None):
        rows = self.queue._queued()
        if request_ids is not None:
            by_id = {r['request']['request_id']: r for r in rows}
            rows = [by_id[key] for key in request_ids]
        return ''.join(m['content'] for r in rows for m in r['request']['params']['event']['messages'])

    def test_codex_and_project_long_prompt_and_final_use_shared_splitter(self):
        text = '測試😀' * 5000
        policy = {'schema': 1, 'enabled': True, 'scopes': [{'cwd': str(self.home),
                  'project_id': 'synthetic-project', 'session_id': 'synthetic-session'}]}
        p = {'device_home': str(self.home), 'cache': str(self.queue.cache),
             'codex_home': str(self.home / '.codex'), 'profile': 'synthetic',
             'profile_sha256': 'a' * 64, 'content_policy_sha256': 'b' * 64,
             'capture_policy': 'synthetic', 'capture_policy_sha256': 'c' * 64, 'content_policy': 'synthetic'}
        p['off_file'] = str(self.home / 'memory.off')
        for kind in ('UserPromptSubmit', 'Stop'):
            result = collection.submit_hook(self.value, self.local, self.payload(kind, text), services=self.services)
            self.assertEqual(self.captured(result['request_ids']), text)
            with patch('agent_brain_client.adapters.storage.pinned.pinned', return_value=policy), \
                 patch.object(hooks, 'validate_hook_policy', return_value=policy), \
                 patch.object(hooks, 'hook_enabled', return_value=True), \
                 patch('agent_brain_client.adapters.server.https.HTTPSContentClient'), \
                 patch('agent_brain_client.domain.content_policy.validate_policy'), \
                 patch('agent_brain_client.adapters.harnesses.codex.content.CodexContentSession', return_value=self.queue):
                result = project_memory.capture(p, self.payload(kind, text, 'project-turn'))
            self.assertEqual(self.captured(result['request_ids']), text)
        self.assertFalse(self.f.cloud.calls)

    def test_long_local_only_excludes_entire_turn_and_never_opens_queue(self):
        for index, text in enumerate(('LocalOnly: ' + 'x' * 9000, 'x' * 9000 + '\nLocalOnly: private')):
            with self.subTest(text=text[:20]), patch.object(self.services, 'adapter') as opened:
                result = collection.submit_hook(self.value, self.local,
                    self.payload('UserPromptSubmit', text, 'turn-' + str(index)), services=self.services)
                self.assertEqual(result['status'], 'excluded-local-only-or-sensitive')
                result = collection.submit_hook(self.value, self.local,
                    self.payload('Stop', 'Synthetic answer derived from private prompt', 'turn-' + str(index)), services=self.services)
                self.assertEqual(result['status'], 'excluded-turn')
                opened.assert_not_called()
        self.assertEqual(self.queue._queued(), [])
        self.assertFalse(list(self.queue.cache.glob('observed-staging/*.json')))

    def test_confirmed_receipts_do_not_consume_active_capacity_or_disappear(self):
        self.stage()
        original = self.queue._queued()[0]
        for index in range(2049):
            row = copy.deepcopy(original)
            request = row['request']
            request['request_id'] = 'synthetic_receipt_' + str(index).zfill(8)
            row.update(request_sha=metadata.digest(contract.encode(request)),
                       state='confirmed', response=accepted(request))
            self.queue._save('outbox/' + request['request_id'] + '.json', row)
        result = self.stage(operation='new-turn')
        self.assertTrue(result['local_queue']['saved'])
        self.assertEqual(self.queue.queue_status()['confirmed'], 2049)
        self.assertEqual(self.queue.queue_status()['pending'], 2)
        self.assertEqual(len(self.queue._queued()), 2051)

    def test_project_long_private_or_restricted_prompt_excludes_following_assistant(self):
        policy = {'schema': 2, 'scopes': [{'cwd': str(self.home),
                  'project_id': 'synthetic-project', 'session_id': 'synthetic-session'}]}
        p = {'profile_sha256': 'a' * 64, 'content_policy_sha256': 'b' * 64,
             'capture_policy': 'synthetic', 'capture_policy_sha256': 'c' * 64,
             'off_file': str(self.home / 'memory.off')}
        for index, prefix in enumerate(('LocalOnly: ', 'do not share this: ') * 2):
            policy['schema'] = 1 if index < 2 else 2
            with patch('agent_brain_client.adapters.storage.pinned.pinned', return_value=policy), \
                 patch.object(hooks, 'validate_hook_policy', return_value=policy), \
                 patch.object(hooks, 'hook_enabled', return_value=True), \
                 patch('agent_brain_client.adapters.server.https.HTTPSContentClient') as client:
                for kind, text in [('UserPromptSubmit', prefix + 'x' * 9000),
                                   ('Stop', 'Synthetic derived assistant reply')]:
                    self.assertEqual(project_memory.capture(p, self.payload(kind, text, 'project-private-' + str(index)))['status'], 'excluded-turn')
                client.assert_not_called()

    def test_every_multibatch_interruption_recovers_exact_tail_without_hook_replay(self):
        text = 'Synthetic prompt ' * 8000 + 'SYNTHETIC_FINAL_TAIL'
        for failure in (1, 2, 3):
            with self.subTest(failure=failure):
                enqueue = self.queue._enqueue
                count = 0
                def interrupted(request):
                    nonlocal count
                    count += 1
                    if count == failure:
                        raise OSError('Synthetic process interruption')
                    return enqueue(request)
                with patch.object(self.queue, '_enqueue', side_effect=interrupted):
                    with self.assertRaises(OSError):
                        self.stage(text=text, operation='turn-' + str(failure))
                journal = next(self.queue.cache.glob('observed-staging/*.json'))
                requests = self.queue._load('observed-staging/' + journal.name)['requests']
                self.queue = self.f.restart()
                self.f.cloud.request = accepted
                self.assertFalse(deliver_pending(self.queue, allowed=lambda _: True))
                ids = [r['request_id'] for r in requests]
                self.assertEqual(self.captured(ids), text)
                for request in requests:
                    self.assertEqual(self.queue._load('outbox/' + request['request_id'] + '.json')['request'], request)
                self.assertFalse(list(self.queue.cache.glob('observed-staging/*.json')))
                self.assertFalse(resume_staging(self.queue))

    def test_full_active_queue_retains_whole_observation_until_capacity_returns(self):
        # Inject capacity refusal before the first row, then let normal delivery
        # recover the observation from disk without another native event.
        text = 'Synthetic long message ' * 9000
        with patch.object(self.queue, '_enqueue', side_effect=CacheBlocked):
            with self.assertRaises(CacheBlocked):
                self.stage(text=text)
        journal = next(self.queue.cache.glob('observed-staging/*.json'))
        requests = self.queue._load('observed-staging/' + journal.name)['requests']
        self.f.cloud.request = accepted
        self.assertFalse(deliver_pending(self.queue, allowed=lambda _: True))
        self.assertEqual(self.captured([r['request_id'] for r in requests]), text)

    def test_real_active_budget_stays_bounded_and_resumes_after_confirmation(self):
        self.stage()
        original = self.queue._queued()[0]
        for index in range(1023):
            row = copy.deepcopy(original)
            request = row['request']
            request['request_id'] = 'synthetic_pending_' + str(index).zfill(8)
            row['request_sha'] = metadata.digest(contract.encode(request))
            self.queue._save('outbox/' + request['request_id'] + '.json', row)
        with self.assertRaises(CacheBlocked):
            self.stage(operation='blocked-by-active-budget')
        self.assertEqual(self.queue.queue_status()['pending'], 1024)
        journal = next(self.queue.cache.glob('observed-staging/*.json'))
        request = self.queue._load('observed-staging/' + journal.name)['requests'][0]
        original.update(state='confirmed', response=accepted(original['request']))
        self.queue._save('outbox/' + original['request']['request_id'] + '.json', original)
        self.assertFalse(resume_staging(self.queue))
        self.assertEqual(self.queue.queue_status()['pending'], 1024)
        self.assertEqual(self.queue._load('outbox/' + request['request_id'] + '.json')['request'], request)

    def test_idle_supervisor_detects_durable_rows_after_hook_dies_before_schedule(self):
        for harness, base in [('codex', 'global-memory'), ('codebuddy', 'codebuddy')]:
            with self.subTest(harness=harness):
                cache = self.home / '.config/agent-brain-client' / base / 'outboxes' / digest(self.local)
                queue = CodexContentSession(self.home, cache, fixture.POLICY, self.f.cloud,
                                           codex_home=self.home / '.codex')
                metadata.write(cache / 'origin.json', self.local, cache=True)
                with patch.object(delivery, 'wake'):
                    delivery.schedule_delivery(self.home, harness, self.local)
                with patch('agent_brain_client.runtime.host_binding.load_core', return_value=Mock()), \
                     patch.object(delivery, 'deliver_origin', return_value=False):
                    delivery.run(self.home)
                stage_messages(queue, project_id='synthetic-project', session_id='synthetic-session',
                    operation_id='new-' + harness, phase='prompt', messages=[{'role': 'user', 'content': 'Synthetic'}])
                delivery.discover_existing(self.home)
                with patch.object(delivery.subprocess, 'Popen') as spawn:
                    self.assertTrue(delivery.wake(self.home))
                    spawn.assert_called_once()

    def test_project_route_is_persisted_before_capture_and_rediscovered(self):
        from agent_brain_client.adapters.harnesses.codex import project_configuration
        p = {'device_home': str(self.home), 'project_root': str(self.home),
             'cache': str(self.queue.cache), 'capture_policy': 'synthetic', 'source_mode': 'codex-project'}
        with patch.object(project_configuration, 'load', return_value=p), patch.object(delivery, 'wake') as wake:
            delivery.schedule_project(self.home, self.home / 'settings.json', 'a' * 64, wake_worker=False)
            wake.assert_not_called()
        task = next(delivery.directory(self.home).glob('*.json'))
        old_time = task.stat().st_mtime_ns
        self.stage()
        with patch.object(project_configuration, 'load', return_value=p):
            delivery.discover_existing(self.home)
        self.assertGreaterEqual(task.stat().st_mtime_ns, old_time)

    def test_splitter_does_not_turn_inline_privacy_example_into_directive(self):
        for marker in ('LocalOnly:', '[private]', 'do not share this:'):
            text = 'x' * 4096 + marker + ' synthetic inline example'
            result = self.stage(text=text, operation='inline-' + digest(marker))
            self.assertEqual(self.captured(result['request_ids']), text)
        self.f.cloud.request = accepted
        self.assertFalse(deliver_pending(self.queue, allowed=lambda _: True))
        self.assertEqual(self.queue.queue_status()['pending'], 0)

    def test_invalid_journal_is_retained_for_review_without_starving_valid_pending(self):
        for index, value in enumerate((None, [], {'schema': 1, 'requests': [], 'requests_sha256': 'changed'})):
            self.queue._save('observed-staging/' + str(index) * 64 + '.json', value)
        malformed = self.queue.cache / 'observed-staging' / ('a' * 64 + '.json')
        malformed.write_bytes(b'{broken synthetic json')
        malformed.chmod(0o600)
        self.stage()
        self.f.cloud.request = accepted
        self.assertFalse(deliver_pending(self.queue, allowed=lambda _: True))
        self.assertEqual(self.queue.queue_status()['confirmed'], 1)
        reviewed = list(self.queue.cache.glob('observed-review/*.json'))
        self.assertEqual(len(reviewed), 4)
        self.assertIn(b'{broken synthetic json', [path.read_bytes() for path in reviewed])

    def test_project_registration_failure_still_recalls_current_question(self):
        import sys
        import io
        p = {'device_home': str(self.home), 'project_root': str(self.home),
             'capture_policy': 'synthetic', 'source_mode': 'codex-project'}
        stream = SimpleNamespace(buffer=io.BytesIO(json.dumps(self.payload('UserPromptSubmit', 'Synthetic question')).encode()))
        with patch.object(sys, 'stdin', stream), \
             patch.object(project_memory, 'active', return_value=True), \
             patch.object(delivery, 'schedule_project', side_effect=OSError), \
             patch.object(project_memory, 'recalled', return_value=[{'content': 'Synthetic recall'}]) as recall, \
             patch.object(project_memory, 'context', return_value='Synthetic recall'):
            result = project_memory.hook_payload(p, settings_path=self.home / 'settings.json', settings_sha256='a' * 64)
        recall.assert_called_once()
        self.assertIn('additionalContext', result['hookSpecificOutput'])


if __name__ == '__main__':
    unittest.main()
