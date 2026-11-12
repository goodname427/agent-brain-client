"""Synthetic delivery scheduler tests; never use installed identity or network."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from agent_brain_client.application.queue.delivery import deliver_pending
from agent_brain_client.runtime import delivery


def row(key='one', state='pending'):
    return {'state': state, 'request': {'request_id': key,
            'method': 'memory_event_submit', 'params': {'event': {'session_id': 'session'}}}}


class Delivery(unittest.TestCase):
    def test_original_request_and_gate_reused(self):
        item = row()
        queue = Mock()
        queue._queued.return_value = [item]
        allowed = Mock(return_value=True)
        def invoke(request, event_gate):
            self.assertIs(request, item['request'])
            self.assertTrue(event_gate())
            item['state'] = 'confirmed'
        queue.invoke.side_effect = invoke
        self.assertFalse(deliver_pending(queue, allowed=allowed))
        self.assertEqual(queue.invoke.call_count, 1)

    def test_revoked_no_send_and_unknown_keeps_pending(self):
        queue = Mock()
        queue._queued.return_value = [row()]
        self.assertTrue(deliver_pending(queue, allowed=lambda _: False))
        queue.invoke.assert_not_called()
        self.assertTrue(deliver_pending(queue, allowed=lambda _: True))
        self.assertEqual(queue.invoke.call_args.args[0]['request_id'], 'one')

    def test_confirmed_and_blocked_never_replayed(self):
        queue = Mock()
        queue._queued.return_value = [row(state='confirmed'), row(state='blocked')]
        self.assertFalse(deliver_pending(queue, allowed=lambda _: True))
        queue.invoke.assert_not_called()

    def test_durable_schedule_and_spawn_storm_suppression(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(delivery.subprocess, 'Popen') as spawn:
            home = Path(temp).resolve()
            delivery.schedule_delivery(home, 'codex', {'cwd': '/synthetic'})
            delivery.schedule_delivery(home, 'codebuddy', {'cwd': '/synthetic'})
            tasks = [p for p in delivery.directory(home).glob('*.json') if p.name not in ('spawn.json', 'status.json')]
            self.assertEqual(len(tasks), 2)
            self.assertEqual(spawn.call_count, 1)
            self.assertNotIn('/synthetic', str(spawn.call_args))

    def test_spawn_failure_retains_task(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(delivery.subprocess, 'Popen', side_effect=OSError):
            home = Path(temp).resolve()
            with self.assertRaises(OSError):
                delivery.schedule_delivery(home, 'codex', {'cwd': '/synthetic'})
            self.assertEqual(len(list(delivery.directory(home).glob('*.json'))), 1)

    def test_worker_failure_retries_without_hook(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            with patch.object(delivery, 'wake'):
                delivery.schedule_delivery(home, 'codex', {'cwd': '/synthetic'})
            with patch('agent_brain_client.runtime.host_binding.load_core', return_value=SimpleNamespace()), \
                 patch.object(delivery, 'deliver_origin', side_effect=[OSError(), False]) as send:
                delivery.run(home, pause=lambda _: None)
                self.assertEqual(send.call_count, 2)
            with patch.object(delivery.subprocess, 'Popen') as spawn:
                self.assertFalse(delivery.wake(home))
                spawn.assert_not_called()

    def test_active_worker_suppresses_another_spawn(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            root = delivery.directory(home)
            delivery.cache_directory(root)
            with delivery.cache_locked(root / 'worker.lock', timeout=0), \
                 patch.object(delivery.subprocess, 'Popen') as spawn:
                self.assertFalse(delivery.wake(home))
                spawn.assert_not_called()

    def test_fair_cursor_skips_previous_unknown_outcome(self):
        queue = Mock()
        queue._queued.return_value = [row('a'), row('b')]
        seen = []
        self.assertTrue(deliver_pending(queue, allowed=lambda _: True,
                                       limit=1, after='a', attempted=seen.append))
        self.assertEqual(seen, ['b'])
        self.assertEqual(queue.invoke.call_args.args[0]['request_id'], 'b')

    def test_live_identity_change_blocks_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            task = {'schema': 1, 'harness': 'codebuddy', 'origin': {'cwd': '/synthetic'}}
            queue = Mock()
            queue._queued.return_value = [row()]
            old = SimpleNamespace(queue_identity={'device': 'old'})
            fresh = SimpleNamespace(queue_identity={'device': 'new'}, status=Mock())
            core = Mock()
            core.load.return_value = {'scope': {'fixture': True}}
            core.approved.return_value = True
            core.build_delivery_queue.side_effect = [(old, queue), (fresh, queue)]
            with patch('agent_brain_client.domain.scopes.select', return_value={}):
                self.assertTrue(delivery.deliver_origin(home, task, core))
            queue.invoke.assert_not_called()
            fresh.status.assert_not_called()

    def test_live_scope_change_blocks_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            task = {'schema': 1, 'harness': 'codebuddy', 'origin': {'cwd': '/synthetic'}}
            queue = Mock()
            queue._queued.return_value = [row()]
            core = Mock()
            core.load.side_effect = [{'scope': {'fixture': 1}}, {'scope': {'fixture': 2}}]
            core.approved.return_value = True
            core.build_delivery_queue.return_value = (Mock(), queue)
            with patch('agent_brain_client.domain.scopes.select', return_value={}):
                self.assertTrue(delivery.deliver_origin(home, task, core))
            queue.invoke.assert_not_called()

    def test_mcp_discovers_original_outbox_origin_without_hook(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            local = {'cwd': '/synthetic'}
            outbox = home / '.config/agent-brain-client/global-memory/outboxes' / delivery.digest(local)
            delivery.cache_directory(outbox)
            delivery.metadata.write(outbox / 'origin.json', local, cache=True)
            delivery.discover_existing(home)
            tasks = list(delivery.directory(home).glob('*.json'))
            self.assertEqual(len(tasks), 1)
            self.assertEqual(delivery.metadata.private_json(tasks[0], cache=True)['origin'], local)
            stamp = tasks[0].stat().st_mtime_ns
            delivery.discover_existing(home)
            self.assertEqual(tasks[0].stat().st_mtime_ns, stamp)

    def test_paused_delivery_resumes_without_marker_or_new_hook(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            with patch.object(delivery, 'wake'):
                delivery.schedule_delivery(home, 'codebuddy', {'cwd': '/synthetic'})
            item = row()
            queue = Mock()
            queue._queued.return_value = [item]
            def sent(request, event_gate):
                self.assertTrue(event_gate())
                item['state'] = 'confirmed'
            queue.invoke.side_effect = sent
            client = Mock(queue_identity={'device': 'same'})
            client.status.return_value = {'source_admitted': True}
            core = Mock()
            core.load.return_value = {'scope': {'fixture': True}}
            core.approved.return_value = False
            core.build_delivery_queue.return_value = (client, queue)
            def resume(_):
                queue.invoke.assert_not_called()
                core.approved.return_value = True
            with patch('agent_brain_client.runtime.host_binding.load_core', return_value=core), \
                 patch('agent_brain_client.domain.scopes.select', return_value={}):
                delivery.run(home, pause=resume)
            self.assertEqual(queue.invoke.call_count, 1)

    def test_observed_completion_recovered_from_marker_at_mcp_start(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            turns = home / '.config/agent-brain-client/codebuddy/turns'
            delivery.cache_directory(turns)
            key = 'a' * 64
            delivery.metadata.write(turns / (key + '.json'), {
                'stopped': True, 'complete': False, 'excluded': False}, cache=True)
            delivery.discover_existing(home)
            task = delivery.metadata.private_json(next(delivery.directory(home).glob('*.json')), cache=True)
            self.assertEqual(task, {'schema': 1, 'harness': 'codebuddy-final', 'turn_id': key})
            with patch('agent_brain_client.runtime.host_binding.load_core', return_value=Mock()), \
                 patch('agent_brain_client.adapters.harnesses.codebuddy.events.resume_observed_assistant',
                       side_effect=[True, False], create=True) as finish:
                delivery.run(home, pause=lambda _: None)
            self.assertEqual(finish.call_count, 2)
            self.assertEqual(finish.call_args.args[2], key)

    def test_project_event_gate_rejects_wrong_project_even_matching_session(self):
        queue = Mock()
        item = row()
        item['request']['params']['event']['project_id'] = 'other'
        queue._queued.return_value = [item]
        self.assertTrue(deliver_pending(queue, allowed=lambda _: True,
            allowed_event=lambda event: event['project_id'] == 'approved'))
        queue.invoke.assert_not_called()

    def test_registration_during_idle_exit_always_wakes_new_worker(self):
        import threading
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            with patch.object(delivery, 'wake'):
                delivery.schedule_delivery(home, 'codex', {'cwd': '/first'})
            original = delivery.metadata.write
            attempted = threading.Event()
            results = []
            workers = []
            def new_task():
                attempted.set()
                results.append(delivery.schedule_delivery(home, 'codex', {'cwd': '/second'}))
            def save(path, value, **kwargs):
                if path.name == 'status.json' and value.get('status') == 'idle':
                    thread = threading.Thread(target=new_task)
                    workers.append(thread)
                    thread.start()
                    self.assertTrue(attempted.wait(2))
                return original(path, value, **kwargs)
            with patch('agent_brain_client.runtime.host_binding.load_core', return_value=Mock()), \
                 patch.object(delivery, 'deliver_origin', return_value=False), \
                 patch.object(delivery.metadata, 'write', side_effect=save), \
                 patch.object(delivery.subprocess, 'Popen') as spawn:
                delivery.run(home)
                for thread in workers:
                    thread.join(3)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(results, [True])
                self.assertEqual(spawn.call_count, 1)

    def test_invalid_completed_binding_stops_retry_without_discarding_task(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            with patch.object(delivery, 'wake'):
                delivery.schedule_assistant_completion(home, 'b' * 64)
            with patch('agent_brain_client.runtime.host_binding.load_core', return_value=Mock()), \
                 patch('agent_brain_client.adapters.harnesses.codebuddy.events.resume_observed_assistant',
                       side_effect=ValueError('Synthetic cancelled request')) as finish:
                delivery.run(home)
                delivery.run(home)
                self.assertEqual(finish.call_count, 1)
            review = next((delivery.directory(home) / 'review').glob('*.json'))
            self.assertEqual(delivery.metadata.private_json(review, cache=True)['status'], 'review-required')
            self.assertTrue(any(p.name not in ('status.json', 'spawn.json')
                                for p in delivery.directory(home).glob('*.json')))

    def test_idle_new_work_resets_cooldown_once_not_per_schedule(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp).resolve()
            with patch.object(delivery, 'wake'):
                delivery.schedule_delivery(home, 'codex', {'cwd': '/old'})
            with patch('agent_brain_client.runtime.host_binding.load_core', return_value=Mock()), \
                 patch.object(delivery, 'deliver_origin', return_value=False):
                delivery.run(home)
            with patch.object(delivery.subprocess, 'Popen') as spawn:
                results = [delivery.schedule_delivery(home, 'codex', {'cwd': '/new' + str(i)})
                           for i in range(5)]
                self.assertEqual(results, [True, False, False, False, False])
                self.assertEqual(spawn.call_count, 1)


if __name__ == '__main__':
    unittest.main()
