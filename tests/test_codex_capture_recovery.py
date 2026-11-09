"""Automatic recovery uses only fictional queues and transports."""
import copy
import unittest
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_event_hook as events
from agent_brain_client.adapters.harnesses.codex.collection import recover_pending


class CaptureRecovery(unittest.TestCase):
    def setUp(self):
        self.fixture = events.EventHook('test_native_prompt_stop_original_outbox_and_replay_without_refresh')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.queue = self.fixture.adapter
        self.value = {'scope': {}}
        self.services = SimpleNamespace(approved=Mock(return_value=True))
        self.client = SimpleNamespace(status=Mock(return_value={'source_admitted': True}))
        self.sent = []
        self.committed = set()
        self.patch = patch('agent_brain_client.adapters.harnesses.codex.collection.select', return_value={})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def stage(self, turn):
        def lost(request):
            self.committed.add(request['params']['id'])
            raise ValueError('Synthetic lost receipt')
        self.fixture.fixture.cloud.request = lost
        self.fixture.prompt(dict(self.fixture.payload, turn_id=turn))
        result = self.fixture.stop(turn_id=turn)
        self.assertEqual(result['local_queue']['state'], 'pending')
        return copy.deepcopy(next(row['request'] for row in self.queue._queued()
                                  if row['request']['request_id'] == result['request_id']))

    def send(self, request):
        self.sent.append(copy.deepcopy(request))
        replayed = request['params']['id'] in self.committed
        self.committed.add(request['params']['id'])
        result = events.accepted(request)
        result['result'].update(replayed=replayed, capture_extraction={
            'state': 'succeeded', 'model_output_truth_verified': False})
        return result

    def recover(self, current='synthetic-current'):
        self.fixture.fixture.cloud.request = self.send
        recover_pending(self.value, {}, self.client, self.queue, current, services=self.services)

    def test_lost_ack_same_identity_receipt_preserved_and_not_duplicated(self):
        request = self.stage('old')
        before = len(self.committed)
        self.recover()
        self.assertEqual(self.sent, [request])
        self.assertEqual(len(self.committed), before)
        row = self.queue._queued()[0]
        self.assertEqual(row['state'], 'confirmed')
        self.assertTrue(row['response']['result']['replayed'])
        self.assertFalse(row['response']['result']['capture_extraction']['model_output_truth_verified'])
        self.recover()
        self.assertEqual(self.sent, [request])

    def test_current_turn_not_immediately_retried(self):
        request = self.stage('current')
        self.recover(request['request_id'])
        self.assertEqual(self.sent, [])

    def test_one_attempt_per_hook_and_cursor_rotates_past_unavailable_event(self):
        requests = sorted([self.stage('a'), self.stage('b')], key=lambda x: x['request_id'])
        def admission(*args):
            if self.client.status.call_count == 1:
                raise ValueError('Synthetic unavailable')
            return {'source_admitted': True}
        self.client.status.side_effect = admission
        self.recover()
        self.assertEqual(self.sent, [])
        self.recover()
        self.assertEqual(self.sent, [requests[1]])
        self.assertEqual(self.queue.queue_status()['pending'], 1)

    def test_disabled_changed_scope_and_denied_admission_never_send(self):
        self.stage('old')
        self.services.approved.return_value = False
        self.recover()
        self.client.status.assert_not_called()
        self.services.approved.return_value = True
        with patch('agent_brain_client.adapters.harnesses.codex.collection.select', return_value=None):
            self.recover()
        self.client.status.assert_not_called()
        self.client.status.return_value = {'source_admitted': False}
        self.recover()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.queue.queue_status()['pending'], 1)

    def test_revocation_while_waiting_for_send_preserves_pending(self):
        self.stage('old')
        self.services.approved.side_effect = [True, False]
        self.recover()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.queue.queue_status()['pending'], 1)

    def test_terminal_rows_and_privacy_marked_events_not_retried(self):
        self.stage('old')
        row = self.queue._queued()[0]
        for state in ('blocked', 'conflict', 'confirmed'):
            with patch.object(self.queue, '_queued', return_value=[dict(row, state=state)]):
                self.recover()
        with patch('agent_brain_client.adapters.harnesses.codex.collection.privacy.private_content', return_value=True):
            self.recover()
        self.assertEqual(self.sent, [])

    def test_stop_hook_wires_recovery_after_capture_but_prompt_does_not(self):
        from agent_brain_client.adapters.harnesses.codex import collection
        local = {'cwd': str(self.fixture.project)}
        value = {'scope': {}, 'device_home': str(self.fixture.fixture.home)}
        runtime = {'https_profile': 'synthetic-profile', 'https_profile_sha256': 'a' * 64,
                   'content_policy_sha256': 'b' * 64}
        services = SimpleNamespace(
            settings_path=lambda _: self.fixture.fixture.base / 'settings.json',
            adapter=Mock(return_value=(self.client, self.queue)),
            approved=Mock(return_value=True))
        for kind in ('Stop', 'UserPromptSubmit'):
            with (
                self.subTest(kind=kind),
                patch.object(collection, 'select', return_value={'sharing': 'personal'}),
                patch.object(collection.registration_store, 'registration', return_value=(None, {'runtime': runtime})),
                patch.object(collection.metadata_io, 'private_json', return_value={'grant': {'actor': 'synthetic-owner'}}),
                patch.object(collection, 'source_id', return_value='synthetic-source'),
                patch('agent_brain_client.adapters.harnesses.codex.hooks.native_hook_fields', return_value=(None, None, kind, 'synthetic text', 'synthetic-key')),
                patch('agent_brain_client.adapters.harnesses.codex.hooks.run_event_hook', return_value={'request_id': 'synthetic-current'}) as capture,
                patch.object(collection, 'recover_pending') as recovery,
            ):
                self.assertEqual(collection.submit_hook(value, local, {}, services=services),
                                 {'request_id': 'synthetic-current'})
                capture.assert_called_once()
                if kind == 'Stop':
                    recovery.assert_called_once_with(value, local, self.client, self.queue,
                                                     'synthetic-current', services=services)
                else:
                    recovery.assert_not_called()

    def test_waiting_recovery_and_content_locks_obey_remaining_budget(self):
        from agent_brain_client.application.control import RequestControl
        from agent_brain_client.adapters.platforms.filesystem import cache_locked
        self.stage('old')
        for filename in ('capture-recovery.lock', 'content.lock'):
            with self.subTest(lock=filename):
                held = threading.Event()
                release = threading.Event()
                def hold():
                    with cache_locked(self.queue.cache / filename):
                        held.set()
                        release.wait(2)
                holder = threading.Thread(target=hold)
                holder.start()
                self.assertTrue(held.wait(1))
                control = RequestControl(0.15)
                started = time.monotonic()
                try:
                    # activate() itself raises on exit at expiry; recovery must
                    # return without submitting even when a different worker owns a lock.
                    try:
                        with control.activate():
                            self.recover()
                    except Exception:
                        pass
                    self.assertLess(time.monotonic() - started, 0.75)
                    self.assertEqual(self.sent, [])
                    self.assertEqual(self.queue.queue_status()['pending'], 1)
                finally:
                    control.close()
                    release.set()
                    holder.join(2)
                self.assertFalse(holder.is_alive())

    def test_concurrent_hooks_do_not_resend_a_confirmed_event(self):
        request = self.stage('old')
        self.fixture.fixture.cloud.request = self.send
        threads = [threading.Thread(target=lambda: recover_pending(
            self.value, {}, self.client, self.queue, 'synthetic-current',
            services=self.services)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.sent, [request])
        self.assertEqual(self.queue.queue_status()['confirmed'], 1)

    def test_local_corruption_does_not_escape_into_current_capture(self):
        self.stage('old')
        with patch.object(self.queue, '_load', side_effect=ValueError('Synthetic corrupt cursor')):
            self.recover()
        self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
