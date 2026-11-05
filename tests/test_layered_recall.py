"""Synthetic shared Mac/Windows recall, with no credentials or native sessions."""
import copy
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
import unicodedata
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / name) for name in ('tools', 'scripts', 'adapters/codex')]
import global_memory as gm
from global_memory_scope import digest, namespace, source_context


class LayeredRecall(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve(); gm.old.imports()
        if os.name == 'nt':
            from brain_windows import protect_new
            protect_new(self.root, directory=True)
        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden'))
        network.start(); self.addCleanup(network.stop)
        self.device, self.peer = 'a' * 32, 'b' * 32
        self.local = self.origin('/synthetic/mac/current')
        self.foreign = self.origin('c:/synthetic/windows/source')
        self.value = {'device_home': str(self.root), 'legacy_projects': [], 'enabled': True,
                      'scope': {'schema': 1, 'profile_id': 'c' * 32,
                                'include_roots': [{'root': '/synthetic/mac', 'sharing': 'personal'}],
                                'exclude_roots': [], 'non_project_roots': [], 'recall_personal': True}}
        pin = digest(self.value['scope'])
        self.value.update(approved_scope_sha256=pin, server_scope_sha256=pin)
        self.project = namespace('synthetic-owner', 'c' * 32)
        self.response = {'result': {'schema': 1, 'namespace_results': [self.group()]}}
        self.sent = []
        client = SimpleNamespace(actor='synthetic-owner', device=self.device,
                                 params=lambda session: {'origin': self.local, 'session_id': session},
                                 wire=lambda method, params, **kwargs: self.send(method, params))
        for target in (patch.object(gm, 'GlobalClient', return_value=client),
                       patch.object(gm, 'settings_path', return_value=self.root/'settings.json')):
            target.start(); self.addCleanup(target.stop)

    @staticmethod
    def origin(root):
        return {'kind': 'project', 'cwd': root, 'project_root': root, 'repository_root': root}

    def send(self, method, params):
        self.sent.append((method, params)); return copy.deepcopy(self.response)

    def row(self, layer):
        return {'id': 'synthetic-' + layer, 'content': 'Synthetic ' + layer + ' answer marker',
                'provenance': {'status': 'verified-native-generation-graph', 'generation_ids': ['generation'],
                               'event_ids': ['event'], 'model_output_truth_verified': False,
                               'sources': [{'event_id': 'event', 'state': 'committed',
                                            'identity': {'device_id': self.peer, 'project_id': self.project}}]},
                'source_context': source_context([self.foreign], self.local)}

    def group(self):
        return {'project_id': self.project, 'sharing': 'personal', 'source_device_id': self.peer,
                'recall': {'L0': [], 'L1': [self.row('L1')], 'L2': [self.row('L2')], 'L3': self.row('L3'),
                           'cross_session': True, 'model_extraction_verified': False}}

    def read(self):
        return gm.recall(self.value, self.local, 'synthetic-reader', 'synthetic question')

    def test_mcp_contract_matches_shared_nfc_source_relation_on_both_platforms(self):
        from content_contract import global_recall_result, recall_origin
        for root in ('/synthetic/projects/caf\u00e9', 'C:\\Synthetic\\Projects\\caf\u00e9'):
            for reader_form, source_form in (('NFC', 'NFD'), ('NFD', 'NFC')):
                with self.subTest(root=root, reader=reader_form, source=source_form):
                    self.local = self.origin(unicodedata.normalize(reader_form, root))
                    self.foreign = self.origin(unicodedata.normalize(source_form, root))
                    group = self.group()
                    self.assertEqual(group['recall']['L3']['source_context']['relation'], 'current-source')
                    params = {'origin':self.local,'session_id':'synthetic-reader','query':'synthetic question','limit':3}
                    global_recall_result({'schema':1,'namespace_results':[group]}, params, self.device)
                    self.assertEqual(recall_origin(self.local), recall_origin(self.foreign))

    def test_mcp_origin_normalizes_before_containment_without_compatibility_folding(self):
        from content_contract import recall_origin
        for root in ('/synthetic/projects/caf\u00e9', 'C:/Synthetic/Projects/caf\u00e9'):
            local = self.origin(root)
            local['cwd'] = unicodedata.normalize('NFD', root) + '/src'
            recall_origin(local)
        # Fullwidth characters are compatibility equivalents, not canonical
        # equivalents. They must remain distinct sources under the NFC contract.
        self.assertNotEqual(recall_origin(self.origin('/synthetic/ProjectA')),
                            recall_origin(self.origin('/synthetic/Project\uff21')))

    def test_both_device_directions_accept_verified_peer_L2_L3(self):
        for local, foreign in ((self.origin('/synthetic/mac/current'), self.origin('c:/synthetic/windows/source')),
                               (self.origin('c:/synthetic/windows/current'), self.origin('/synthetic/mac/source'))):
            self.local, self.foreign = local, foreign
            self.value['scope']['include_roots'] = [{'root': local['project_root'], 'sharing': 'personal'}]
            self.response['result']['namespace_results'] = [self.group()]
            rows = self.read()
            self.assertEqual([r['layer'] for r in rows], ['L1', 'L2', 'L3'])
            self.assertTrue(all(r['source_context']['relation'] == 'other-source' for r in rows))
            self.assertTrue(all(r['source_context']['use'] == 'reference-only' for r in rows))
        self.assertTrue(all(method == 'memory_query' for method, _ in self.sent))

    def test_legacy_local_reply_checks_local_origins(self):
        group = self.group(); group.pop('source_device_id')
        for row in [*group['recall']['L1'], *group['recall']['L2'], group['recall']['L3']]:
            row['provenance']['sources'][0]['identity']['device_id'] = self.device
            row['source_context'] = source_context([self.local], self.local)
        self.response['result']['namespace_results'] = [group]
        self.assertEqual(len(self.read()), 3)
        group['recall']['L3']['source_context'] = source_context([self.foreign], self.local)
        with self.assertRaises(ValueError): self.read()

    def test_peer_scope_provenance_and_applicability_fail_closed(self):
        changes = [lambda g: g.update(sharing='project'), lambda g: g.update(source_device_id='bad-device'),
                   lambda g: g.update(project_id='foreign-project'), lambda g: g.update(debug='unknown'),
                   lambda g: g['recall']['L3']['provenance']['sources'][0]['identity'].update(device_id=self.device),
                   lambda g: g['recall']['L3']['provenance'].update(sources=[]),
                   lambda g: g['recall']['L3']['source_context'].update(global_rule=True)]
        for index, change in enumerate(changes):
            self.response['result']['namespace_results'] = [self.group()]
            change(self.response['result']['namespace_results'][0])
            with self.subTest(change=index), self.assertRaises(ValueError): self.read()
        self.response['result']['namespace_results'] = [self.group()]
        self.value['scope']['recall_personal'] = False
        with self.assertRaises(ValueError): self.read()

    def test_hook_receipts_preserve_more_than_three_native_results(self):
        self.response['result']['namespace_results']=[self.group(),self.group()]
        result=self.run_hook()
        self.assertTrue(result)
        receipt=gm.recall_observation(self.value)
        self.assertEqual(receipt['item_count'],6)
        self.assertEqual(len(receipt['retrieval']['selected']),6)

    def test_empty_peer_groups_preserve_native_per_namespace_results(self):
        empty = self.group(); empty['recall'].update(L1=[], L2=[], L3={'content': None, 'provenance': {}})
        self.response['result']['namespace_results'] = [empty, empty, self.group()]
        self.assertEqual(len(self.read()), 3)
        self.response['result']['namespace_results'] = [self.group(), self.group()]
        self.assertEqual(len(self.read()), 6)
        self.response['result']['namespace_results'] = [empty] * 19
        with self.assertRaises(ValueError): self.read()

    def prompt(self):
        payload = {'cwd': self.local['cwd'], 'hook_event_name': 'UserPromptSubmit',
                   'session_id': 'synthetic-reader', 'turn_id': 'synthetic-turn', 'prompt': 'Synthetic question marker'}
        return SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))

    def run_hook(self, **kwargs):
        with patch.object(gm, 'eligible', return_value=self.local), patch.object(gm, 'submit_hook', return_value={'status': 'staged'}), patch.object(sys, 'stdin', self.prompt()):
            return gm.hook(self.value, **kwargs)

    def test_hook_prepares_L2_L3_without_claiming_model_use(self):
        observed = []; result = self.run_hook(observe=observed.append)
        context = result['hookSpecificOutput']['additionalContext']
        self.assertIn('Synthetic L2 answer marker', context); self.assertIn('Synthetic L3 answer marker', context)
        receipt = gm.recall_observation(self.value)
        self.assertEqual(receipt, observed[0]); self.assertEqual(receipt['layer_counts'], {'L1': 1, 'L2': 1, 'L3': 1})
        self.assertEqual(receipt['status'], 'context-prepared'); self.assertEqual(receipt['model_used'], 'unknown')
        self.assertEqual(receipt['context_bytes'], len(context.encode()))
        for forbidden in ('answer marker', 'question marker', 'synthetic-reader', '/synthetic', self.peer):
            self.assertNotIn(forbidden, json.dumps(receipt))
        self.assertEqual(self.run_hook(), {}); self.assertEqual(len(self.sent), 1)

    def test_empty_recall_and_unobserved_are_distinct(self):
        self.assertEqual(gm.recall_observation(self.value), {'status': 'unobserved', 'model_used': 'unknown'})
        self.response['result']['namespace_results'] = []
        self.assertEqual(self.run_hook(), {})
        receipt = gm.recall_observation(self.value)
        self.assertEqual(receipt['status'], 'empty'); self.assertEqual(receipt['item_count'], 0)
        self.assertEqual(receipt['context_bytes'], 0)

    def test_cjk_budget_uses_delivered_utf8_and_keeps_whole_facts(self):
        group = self.response['result']['namespace_results'][0]
        for layer in ('L1', 'L2', 'L3'):
            row = group['recall'][layer] if layer == 'L3' else group['recall'][layer][0]
            row['content'] = '虚构中文记忆。' * 200
        self.assertGreater(len(json.dumps(group).encode()), 24576)
        result = self.run_hook()
        self.assertTrue(result)
        self.assertLessEqual(len(result['hookSpecificOutput']['additionalContext'].encode()), 24576)
        receipt = gm.recall_observation(self.value)
        self.assertEqual(receipt['item_count'], 3)
        self.assertEqual(receipt['retrieval']['omitted_for_budget'], 0)

    def test_oversized_context_skips_whole_row_and_retains_other_results(self):
        group = self.response['result']['namespace_results'][0]
        group['recall']['L1'][0]['content'] = 'x' * 25000
        result = self.run_hook()
        self.assertIn('Synthetic L2 answer marker', result['hookSpecificOutput']['additionalContext'])
        receipt = gm.recall_observation(self.value)
        self.assertEqual(receipt['retrieval']['candidate_count'], 3)
        self.assertEqual(receipt['retrieval']['omitted_for_budget'], 1)
        self.assertEqual(len(receipt['retrieval']['returned']), 3)
        self.assertNotIn(receipt['retrieval']['returned'][0], receipt['retrieval']['selected'])
        self.assertEqual(receipt['item_count'], 2)

    def test_request_receipt_survives_later_query_and_failed_query_has_receipt(self):
        self.run_hook()
        first = gm.recall_observation(self.value)
        self.assertFalse(first['output_written'])
        self.assertEqual(first['query_sha256'], digest('Synthetic question marker'))
        self.assertEqual(first['retrieval']['selected'][0]['content_sha256'], digest('Synthetic L1 answer marker'))
        self.assertEqual(first['retrieval']['selected'][0]['record_sha256'], digest('synthetic-L1'))
        original = self.prompt
        def next_prompt():
            data = json.loads(original().buffer.read()); data['turn_id'] += '-next'
            return SimpleNamespace(buffer=io.BytesIO(json.dumps(data).encode()))
        with patch.object(self, 'prompt', side_effect=next_prompt), patch.object(gm, 'recall', side_effect=ValueError('FICTIONAL_PRIVATE_ERROR')):
            with self.assertRaises(ValueError): self.run_hook()
        failed = gm.recall_observation(self.value)
        self.assertEqual(failed['status'], 'failed'); self.assertEqual(failed['error_type'], 'ValueError')
        self.assertNotEqual(first['request_key'], failed['request_key'])
        self.assertEqual(gm.recall_observation(self.value, first['request_key']), first)
        self.assertNotIn('FICTIONAL_PRIVATE_ERROR', json.dumps(failed))

    def test_cli_observes_stdout_and_redacts_failure(self):
        output, diagnostic = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['global_memory', 'global-hook']), patch.object(gm, 'load', return_value=self.value), patch.object(gm, 'eligible', return_value=self.local), patch.object(gm, 'submit_hook', return_value={'status': 'staged'}), patch.object(sys, 'stdin', self.prompt()), patch.object(sys, 'stdout', output), patch.object(sys, 'stderr', diagnostic):
            self.assertEqual(gm.main(), 0)
        self.assertIn('additionalContext', output.getvalue())
        receipt = json.loads(diagnostic.getvalue())
        self.assertEqual(receipt['status'], 'hook-output-written'); self.assertEqual(receipt['recall_status'], 'context-prepared')
        self.assertTrue(gm.recall_observation(self.value, receipt['request_key'])['output_written'])
        self.assertNotIn('answer marker', diagnostic.getvalue()); self.assertEqual(receipt['model_used'], 'unknown')
        with patch.object(sys, 'argv', ['global_memory', 'global-hook']), patch.object(gm, 'load', side_effect=RuntimeError('SYNTHETIC_SECRET')), patch.object(sys, 'stdout', io.StringIO()), patch.object(sys, 'stderr', io.StringIO()) as errors:
            self.assertEqual(gm.main(), 0)
        self.assertNotIn('SYNTHETIC_SECRET', errors.getvalue()); self.assertNotIn('hook-output-written', errors.getvalue())


if __name__ == '__main__': unittest.main()
