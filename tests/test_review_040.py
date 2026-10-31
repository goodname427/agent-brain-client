"""Regression checks for interrupted installs and finite contextual recall."""
import copy
import io
import json
import unittest
from unittest.mock import patch

import agentbrain_040 as entry
import content_install as install
import test_content_install as install_fixtures
import test_device_task as task_fixtures
from https_content import HTTPSContentClient


class InstallRecovery(unittest.TestCase):
    def setUp(self):
        self.f = install_fixtures.ContentInstall('test_interrupted_apply_recovers_before_retry')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.plan = self.f.plan()
        self.digest = self.f.digest(self.plan)
        self.path = self.f.base / 'private-install-plan.json'
        install.save(self.path, self.plan)
        real_apply = install.apply
        # Only replace the protocol probe; exercise the actual durable transaction.
        probe = patch.object(install, 'apply', side_effect=lambda p, d: real_apply(p, d, lambda _: None))
        probe.start()
        self.addCleanup(probe.stop)

    def run_entry(self):
        with patch('sys.stdout', new=io.StringIO()) as output:
            code = entry.main(['apply', '--plan-file', str(self.path), '--plan-sha256', self.digest])
        return code, json.loads(output.getvalue())

    def interrupt(self):
        real_atomic = install.atomic
        def crash(path, *args):
            real_atomic(path, *args)
            if path == self.f.config:
                raise SystemExit('Synthetic interruption after config write')
        with patch.object(install, 'atomic', side_effect=crash):
            with self.assertRaises(SystemExit):
                self.run_entry()
        receipt = self.f.home / '.config/agent-brain-client/content-install-receipt.json'
        self.assertEqual(json.loads(receipt.read_bytes())['phase'], 'applying')

    def test_same_private_plan_recovers_then_completed_plan_is_idempotent(self):
        self.interrupt()
        code, result = self.run_entry()
        self.assertEqual(code, 0, result)
        self.assertTrue(install.inspect(self.f.home)['installed'])
        before = self.f.native_files()
        code, result = self.run_entry()
        self.assertEqual(code, 0, result)
        self.assertEqual(self.f.native_files(), before)

    def test_external_edit_after_interruption_is_preserved(self):
        self.interrupt()
        self.f.config.write_text(self.f.config.read_text() + '\nexternal_review_marker="preserve"\n')
        before = self.f.native_files()
        code, result = self.run_entry()
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'content-install-recovery-review-required')
        self.assertEqual(self.f.native_files(), before)


class RecallPolicy(unittest.TestCase):
    def setUp(self):
        self.f = task_fixtures.DeviceTask()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def unpinned_client(self):
        return HTTPSContentClient(self.f.f.profile_path, self.f.sha, store=self.f.f.store)

    def test_context_without_selection_is_denied_before_custody_or_transport(self):
        client = self.unpinned_client()
        self.assertNotIn('selection', self.f.recall['params'])
        with patch.object(self.f.f.store, 'load', side_effect=AssertionError('No credential IO')) as load, \
             patch.object(client.transport, 'request') as request:
            result = client.request(self.f.recall)
        self.assertEqual(result['error']['code'], 'device_transport_blocked')
        load.assert_not_called()
        request.assert_not_called()

    def test_legacy_query_remains_available_without_a_finite_task(self):
        client = self.unpinned_client()
        query = copy.deepcopy(self.f.recall)
        del query['params']['context']
        expected = {'request_id': query['request_id'], 'result': {'fixture': 'legacy query'}}
        with patch.object(client.transport, 'request', return_value=expected) as request:
            self.assertEqual(client.request(query), expected)
        request.assert_called_once_with(query)

    def test_pinned_contextual_recall_still_rejects_an_invalid_response(self):
        self.f.response = {'request_id': self.f.recall['request_id'], 'result': {'unvalidated': True}}
        result = self.f.client.request(self.f.recall)
        self.assertIn('error', result)
        self.assertEqual(len(self.f.calls), 1)


if __name__ == '__main__':
    unittest.main()
