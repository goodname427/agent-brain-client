"""Explicit recovery preserves the request through denial and lost ACK."""
import copy
import unittest
import test_content_outbox as fixture
from content_cache import CacheBlocked


class BlockedRetry(unittest.TestCase):
    def setUp(self):
        self.f = fixture.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.original = self.f.cloud.request
        self.request = self.f.request()

    def block(self):
        def denied(request):
            if request['method'] == 'content_put':
                return {'request_id': request['request_id'], 'error': {'code': 'invalid_request', 'retryable': False}, 'persistence': {'durable': False}}
            return self.original(request)
        self.f.cloud.request = denied
        self.assertEqual(self.f.adapter.invoke(self.request)['local_queue']['state'], 'blocked')
        self.path = self.f.cache / 'outbox' / (self.request['request_id'] + '.json')
        self.f.cloud.request = self.original

    def test_only_explicit_identical_retry_unblocks_and_maps(self):
        self.block()
        before = self.path.read_bytes()
        restarted = self.f.restart()
        self.assertEqual(restarted.refresh()['status'], 'content-local-changes-retained')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.f.cloud.puts, 0)
        result = restarted.invoke(self.request)
        self.assertEqual(result['local_queue']['state'], 'confirmed')
        self.assertEqual(result['local_refresh']['status'], 'content-refreshed')
        self.assertEqual(self.f.cloud.puts, 1)
        self.assertEqual([r for r in self.f.cloud.calls if r['method'] == 'content_put'], [self.request])
        self.assertEqual(restarted._queued()[0]['request'], self.request)
        self.assertEqual(restarted.invoke(self.request)['result'], result['result'])
        self.assertEqual(self.f.cloud.puts, 1)

    def test_lost_recovery_ack_preserves_blocked_then_recovers_once(self):
        self.block()
        before = self.path.read_bytes()
        self.f.cloud.lose_ack = True
        result = self.f.adapter.invoke(self.request)
        self.assertEqual(result['local_queue']['state'], 'blocked')
        self.assertFalse(result['local_queue']['server_saved'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.f.cloud.puts, 1)
        self.assertEqual(self.f.restart().invoke(self.request)['local_queue']['state'], 'confirmed')
        self.assertEqual(self.f.cloud.puts, 1)

    def test_local_ack_is_valid_and_explicit_retry_sends_only_once(self):
        self.block()
        def local(request):
            response = self.original(request)
            if request['method'] == 'content_put' and 'error' not in response:
                response = copy.deepcopy(response)
                response['persistence'].update(state='local-confirmed', durable=False,
                                               local_durable=True, github_confirmed=False)
            return response
        self.f.cloud.request = local
        result = self.f.adapter.invoke(self.request)
        self.assertEqual(result['local_queue']['state'], 'confirmed')
        self.assertTrue(result['local_queue']['server_saved'])
        self.assertFalse(result['local_queue']['github_confirmed'])
        self.assertEqual([r for r in self.f.cloud.calls if r['method'] == 'content_put'], [self.request])

    def test_changed_parameters_or_binding_never_resend(self):
        self.block()
        before = self.path.read_bytes()
        with self.assertRaises(CacheBlocked):
            self.f.adapter.invoke(self.f.request(body='Different candidate'))
        self.f.adapter.queue_sha = 'a' * 64
        with self.assertRaises(CacheBlocked):
            self.f.adapter.invoke(self.request)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.f.cloud.puts, 0)

    def test_recovery_conflict_uses_existing_reconciliation(self):
        self.block()
        remote = dict(fixture.MEMORY, body='Another writer saved this')
        self.f.cloud.records[('memory', 'synthetic-topic')] = remote
        result = self.f.adapter.invoke(self.request)
        self.assertEqual(result['local_queue']['state'], 'conflict')
        row = self.f.adapter._queued()[0]
        self.assertEqual(row['request'], self.request)
        self.assertEqual(row['remote']['record'], remote)
        merged = self.f.request('synthetic_recovery_merged_001', body='Merged both versions', revision=row['remote']['revision'])
        result = self.f.adapter.invoke(merged, reconciles={'request_ids': [self.request['request_id']], 'source_keys': []})
        self.assertEqual(result['local_queue']['state'], 'confirmed')
        self.assertEqual(self.f.adapter.queue_status()['reconciled'], 1)

    def test_contradictory_ack_cannot_unblock(self):
        self.block()
        before = self.path.read_bytes()
        def corrupt(request):
            result = self.original(request)
            if request['method'] == 'content_put':
                result = copy.deepcopy(result)
                result['result']['record']['body'] = 'Not the submitted record'
            return result
        self.f.cloud.request = corrupt
        result = self.f.adapter.invoke(self.request)
        self.assertEqual(result['local_queue']['state'], 'blocked')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(result['local_queue']['server_saved'])


if __name__ == '__main__':
    unittest.main()
