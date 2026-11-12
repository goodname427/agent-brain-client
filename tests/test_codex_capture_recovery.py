"""Automatic recovery uses only fictional queues and transports."""
import copy
import unittest
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_event_hook as events
from agent_brain_client.application.queue.delivery import deliver_pending


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
        self.allowed_scope = True
        self.after = ''

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

    def recover(self, *, limit=16):
        self.fixture.fixture.cloud.request = self.send
        def allowed(session):
            return (self.services.approved() and self.allowed_scope
                    and self.client.status(session).get('source_admitted') is True)
        def attempted(key):
            self.after = key
        return deliver_pending(self.queue, allowed=allowed, limit=limit,
                               after=self.after, attempted=attempted)

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

    def test_bounded_background_pass_rotates_past_unavailable_event(self):
        requests = sorted([self.stage('a'), self.stage('b')], key=lambda x: x['request_id'])
        def admission(*args):
            if self.client.status.call_count == 1:
                raise ValueError('Synthetic unavailable')
            return {'source_admitted': True}
        self.client.status.side_effect = admission
        with self.assertRaises(ValueError):
            self.recover(limit=1)
        self.assertEqual(self.sent, [])
        self.recover(limit=1)
        self.assertEqual(self.sent, [requests[1]])
        self.assertEqual(self.queue.queue_status()['pending'], 1)

    def test_disabled_changed_scope_and_denied_admission_never_send(self):
        self.stage('old')
        self.services.approved.return_value = False
        self.recover()
        self.client.status.assert_not_called()
        self.services.approved.return_value = True
        self.allowed_scope = False
        self.recover()
        self.allowed_scope = True
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
        with patch('agent_brain_client.application.queue.delivery.privacy.private_content', return_value=True):
            self.recover()
        self.assertEqual(self.sent, [])

    def test_waiting_recovery_and_content_locks_obey_remaining_budget(self):
        from agent_brain_client.application.control import RequestControl
        from agent_brain_client.adapters.platforms.filesystem import cache_locked
        self.stage('old')
        for filename in ('content.lock',):
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
        threads = [threading.Thread(target=self.recover) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.sent, [request])
        self.assertEqual(self.queue.queue_status()['confirmed'], 1)

    def test_local_corruption_does_not_escape_into_current_capture(self):
        self.stage('old')
        with patch.object(self.queue, '_load', side_effect=ValueError('Synthetic corrupt queue')), self.assertRaises(ValueError):
            self.recover()
        self.assertEqual(self.sent, [])


if __name__ == '__main__':
    unittest.main()
