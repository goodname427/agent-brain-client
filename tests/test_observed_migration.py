"""Only existing observed staging migrates; original receipts never change."""
import json
import unittest

import test_event_hook as events
from agent_brain_client.adapters.storage.observed_migration import migrate_staged_turns


class ObservedMigration(unittest.TestCase):
    def setUp(self):
        self.fixture = events.EventHook('test_native_prompt_stop_original_outbox_and_replay_without_refresh')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.queue = self.fixture.adapter

    def migrate(self):
        migrate_staged_turns(self.queue, origin={'cwd': str(self.fixture.project)},
                             project_id='synthetic-project')

    def test_prompt_survives_without_final_and_preserves_time(self):
        self.fixture.prompt()
        original = next((self.queue.cache / 'hook-turns').glob('*.json')).read_bytes()
        self.migrate()
        rows = self.queue._queued()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['state'], 'pending')
        self.assertEqual(rows[0]['request']['params']['event']['occurred_at'], '2026-10-17T12:00:00.000Z')
        self.assertEqual(rows[0]['request']['params']['event']['messages'],
                         [{'role': 'user', 'content': self.fixture.payload['prompt']}])
        self.migrate()
        self.assertEqual(len(self.queue._queued()), 1)
        self.assertEqual(next((self.queue.cache / 'hook-turns').glob('*.json')).read_bytes(), original)
        self.assertEqual(self.fixture.sent, [])

    def test_existing_pair_outbox_is_not_recreated(self):
        self.fixture.prompt()
        self.fixture.stop()
        rows = self.queue._queued()
        before = json.dumps(rows, sort_keys=True)
        self.migrate()
        self.assertEqual(json.dumps(self.queue._queued(), sort_keys=True), before)

    def test_staged_final_before_enqueue_crash_migrates_both(self):
        self.fixture.prompt()
        file = next((self.queue.cache / 'hook-turns').glob('*.json'))
        row = self.queue._load('hook-turns/' + file.name)
        row['final'] = 'Synthetic completed answer'
        self.queue._save('hook-turns/' + file.name, row)
        self.migrate()
        roles = sorted(r['request']['params']['event']['messages'][0]['role'] for r in self.queue._queued())
        self.assertEqual(roles, ['assistant', 'user'])
        self.migrate()
        self.assertEqual(len(self.queue._queued()), 2)

    def test_wrong_scope_does_not_create_event(self):
        self.fixture.prompt()
        migrate_staged_turns(self.queue, origin={'cwd': '/wrong'}, project_id='synthetic-project')
        self.assertEqual(self.queue._queued(), [])

    def test_unrelated_old_stage_does_not_block_current_queue(self):
        self.fixture.prompt()
        from agent_brain_client.application.memory.observations import stage_messages
        stage_messages(self.queue, project_id='synthetic-new-project', session_id='new-session',
                       operation_id='new-turn', phase='prompt',
                       messages=[{'role': 'user', 'content': 'Synthetic current content'}])
        migrate_staged_turns(self.queue, origin={'cwd': str(self.fixture.project)},
                             project_id='synthetic-new-project')
        rows = self.queue._queued()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['request']['params']['event']['project_id'], 'synthetic-new-project')
