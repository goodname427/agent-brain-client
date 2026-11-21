import agent_brain_client.interfaces.cli.collection as collection_cli
import agent_brain_client.adapters.storage.recall_receipts as recall_receipts
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
sys.path[:0] = [str(ROOT / name) for name in ('src', 'src', 'src', 'src')]
import agent_brain_client.runtime.collection as gm
from agent_brain_client.domain.scopes import digest, namespace, source_context


class LayeredRecall(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve();
        if os.name == 'nt':
            from agent_brain_client.adapters.platforms.windows.native import protect_new
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
        for target in (patch.object(gm, 'schedule_delivery', return_value=False),
                       patch.object(gm, 'CollectionClient', return_value=client),
                       patch.object(recall_receipts, 'settings_path', return_value=self.root/'settings.json'),
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

    def test_sharing_questions_survive_query_and_verified_layers(self):
        from agent_brain_client.domain.contract import global_recall_params, global_recall_result
        group = self.response['result']['namespace_results'][0]
        for question in ('为什么规则禁止跨项目共享？', '禁止跨项目共享是什么意思？',
                         'What does keep this project-only mean?'):
            with self.subTest(question=question):
                params = {'origin': self.local, 'session_id': 'synthetic-reader', 'query': question, 'limit': 3}
                for row in [*group['recall']['L1'], *group['recall']['L2'], group['recall']['L3']]:
                    row['content'] = question
                global_recall_params(params)
                global_recall_result(self.response['result'], params, self.device)
                rows = gm.recall(self.value, self.local, params['session_id'], question)
                self.assertEqual([row['content'] for row in rows], [question] * 3)
                self.assertEqual(self.sent[-1][1]['query'], question)

    def test_body_shape_preserves_query_and_verified_recall_text(self):
        from agent_brain_client.domain.contract import global_recall_params, global_recall_result
        text = 'Synthetic documentation password=example-only; Bearer ' + 'synthetic' * 4
        group = self.response['result']['namespace_results'][0]
        for row in [*group['recall']['L1'], *group['recall']['L2'], group['recall']['L3']]:
            row['content'] = text
        params = {'origin': self.local, 'session_id': 'synthetic-reader', 'query': text, 'limit': 3}
        global_recall_params(params)
        rows = gm.recall(self.value, self.local, params['session_id'], text)
        self.assertEqual([row['content'] for row in rows], [text] * 3)
        self.assertEqual(self.sent[0][1]['query'], text)
        for marker in ('[no-memory]', '[project-only]', '不要记忆：'):
            with self.subTest(marker=marker):
                with self.assertRaises(ValueError):
                    global_recall_params(dict(params, query=marker + text))
                group['recall']['L3']['content'] = marker + text
                with self.assertRaises(ValueError):
                    global_recall_result(self.response['result'], params, self.device)
                self.assertEqual([row['layer'] for row in self.read()], ['L1', 'L2'])
                group['recall']['L3']['content'] = text

    def test_mcp_contract_matches_shared_nfc_source_relation_on_both_platforms(self):
        from agent_brain_client.domain.contract import global_recall_result, recall_origin
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
        from agent_brain_client.domain.contract import recall_origin
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
        receipt=recall_receipts.recall_observation(self.value)
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

    def prompt(self, question='Synthetic question marker'):
        payload = {'cwd': self.local['cwd'], 'hook_event_name': 'UserPromptSubmit',
                   'session_id': 'synthetic-reader', 'turn_id': 'synthetic-turn', 'prompt': question}
        return SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))

    def run_hook(self, *, question=None, **kwargs):
        prompt = self.prompt() if question is None else self.prompt(question)
        with patch.object(gm, 'eligible', return_value=self.local), patch.object(gm, 'submit_hook', return_value={'status': 'staged'}), patch.object(sys, 'stdin', prompt):
            return gm.hook(self.value, **kwargs)

    def fitted_hook(self, question):
        output = self.run_hook(question=question)
        context = output['hookSpecificOutput']['additionalContext']
        receipt = recall_receipts.recall_observation(self.value)
        self.assertEqual(receipt['context_sha256'], digest(context))
        self.assertEqual(receipt['context_bytes'], len(context.encode('utf-8')))
        self.assertLessEqual(receipt['context_bytes'], 24576)
        return [json.loads(line) for line in context.split('\n')[1:]], receipt

    def test_hook_prepares_L2_L3_without_claiming_model_use(self):
        observed = []; result = self.run_hook(observe=observed.append)
        context = result['hookSpecificOutput']['additionalContext']
        self.assertIn('Synthetic L2 answer marker', context); self.assertIn('Synthetic L3 answer marker', context)
        receipt = recall_receipts.recall_observation(self.value)
        self.assertEqual(receipt, observed[0]); self.assertEqual(receipt['layer_counts'], {'L1': 1, 'L2': 1, 'L3': 1})
        self.assertEqual(receipt['status'], 'context-prepared'); self.assertEqual(receipt['model_used'], 'unknown')
        self.assertEqual(receipt['context_bytes'], len(context.encode()))
        for forbidden in ('answer marker', 'question marker', 'synthetic-reader', '/synthetic', self.peer):
            self.assertNotIn(forbidden, json.dumps(receipt))
        # Prepared output may be replayed until stdout has actually flushed.
        self.assertTrue(self.run_hook()); self.assertEqual(len(self.sent), 2)
        receipt['output_written'] = True
        recall_receipts.save_recall_observation(self.value, receipt)
        self.assertEqual(self.run_hook(), {}); self.assertEqual(len(self.sent), 2)

    def test_empty_recall_and_unobserved_are_distinct(self):
        self.assertEqual(recall_receipts.recall_observation(self.value), {'status': 'unobserved', 'model_used': 'unknown'})
        self.response['result']['namespace_results'] = []
        self.assertEqual(self.run_hook(), {})
        receipt = recall_receipts.recall_observation(self.value)
        self.assertEqual(receipt['status'], 'empty'); self.assertEqual(receipt['item_count'], 0)
        self.assertEqual(receipt['context_bytes'], 0)

    def test_cjk_budget_uses_delivered_utf8_and_keeps_whole_facts(self):
        group = self.response['result']['namespace_results'][0]
        content = '虚构中文记忆。' * 57
        for layer in ('L1', 'L2', 'L3'):
            row = group['recall'][layer] if layer == 'L3' else group['recall'][layer][0]
            row['content'] = content
        groups = self.response['result']['namespace_results']
        groups.extend(copy.deepcopy(group) for _ in range(3))
        self.assertGreater(len(json.dumps(groups).encode()), 24576)
        wire = io.BytesIO()
        with (io.TextIOWrapper(wire, encoding='utf-8') as output,
              patch.object(gm, 'load', return_value=self.value),
              patch.object(gm, 'eligible', return_value=self.local),
              patch.object(gm, 'submit_hook', return_value={'status': 'staged'}),
              patch.object(sys, 'stdin', self.prompt('虚构中文记忆是什么？')),
              patch.object(sys, 'stdout', output),
              patch.object(sys, 'stderr', io.StringIO())):
            self.assertEqual(collection_cli.main(['global-hook']), 0)
            context = json.loads(wire.getvalue())['hookSpecificOutput']['additionalContext']
        receipt = recall_receipts.recall_observation(self.value)
        self.assertTrue(receipt['output_written'])
        self.assertEqual(receipt['context_bytes'], len(context.encode('utf-8')))
        self.assertLessEqual(receipt['context_bytes'], 24576)
        self.assertEqual(receipt['context_sha256'], digest(context))
        self.assertEqual(receipt['item_count'], 12)
        self.assertEqual([json.loads(line)['content'] for line in context.split('\n')[1:]],
                         [content] * 12)
        self.assertEqual(receipt['retrieval']['omitted_for_budget'], 0)

    def test_context_budget_counts_every_rendered_row_and_separator(self):
        from agent_brain_client.application.memory.context import (
            select_context_items, render_context,
        )

        first = {'content': '界' * 6500}
        second = {'content': 'a'}
        base_bytes = len(render_context([first, second]).encode('utf-8'))
        second['content'] += 'x' * (24576 - base_bytes)
        # The first row's repeated object cannot reenter the output over budget.
        self.assertEqual(select_context_items([first, first, second], [], '', cached=True),
                         ({0: first, 2: second}, 0))
        self.assertEqual(len(render_context([first, second]).encode('utf-8')), 24576)
        second['content'] += 'x'
        self.assertEqual(select_context_items([first, second], [], '', cached=True), ({0: first}, 0))

    def test_budget_keeps_small_current_source_evidence_after_two_long_l1_rows(self):
        from agent_brain_client.application.memory.context import render_context

        group = self.response['result']['namespace_results'][0]
        first = dict(self.row('L1'), id='synthetic-first', content='Background. ' + 'x' * 11000)
        second = dict(self.row('L1'), id='synthetic-second', content='Quartz history. ' + 'y' * 11000)
        target = dict(self.row('L2'), content='Quartz beacon code is UMBER.',
                      source_context=source_context([self.local], self.local))
        group['recall'].update(L1=[first, second], L2=[], L3={'content': None, 'provenance': {}})
        rows = [
            dict(scope='personal', layer='L1', content=row['content'],
                 source_context=row['source_context'], relevance='native-query-match')
            for row in (first, second)
        ]
        room = 24576 - len(render_context(rows).encode('utf-8'))
        self.assertGreater(room, 32)
        second['content'] += 'y' * (room - 32)
        group['recall']['L2'] = [target]

        rows, receipt = self.fitted_hook('What is the Quartz beacon code?')
        self.assertEqual(rows[0]['content'], target['content'])
        self.assertEqual(rows[0]['source_context'], target['source_context'])
        self.assertEqual(receipt['layer_counts'], {'L1': 2, 'L2': 1, 'L3': 0})
        self.assertEqual(receipt['retrieval']['selected'][0]['record_sha256'], digest(target['id']))
        self.assertEqual([ref['record_sha256'] for ref in receipt['retrieval']['selected'][1:]],
                         [digest(first['id']), digest(second['id'])])
        self.assertEqual(self.sent[-1][1]['limit'], 5)
        self.assertEqual(len(self.sent), 1)

    def test_selects_and_presents_evidence_below_budget(self):
        group = self.response['result']['namespace_results'][0]
        semantic = dict(self.row('L1'), id='synthetic-semantic',
                        content='The night crew checks the secondary lantern. ' + '背景' * 250)
        target = dict(self.row('L2'), id='synthetic-answer', content='The Kestrel beacon is violet.',
                      source_context=source_context([self.local], self.local))
        key_long = dict(self.row('L2'), id='synthetic-long', content='背景' * 250 + target['content'])
        noise = dict(self.row('L2'), id='synthetic-noise', content='Fictional garden instructions.')
        group['recall'].update(L1=[semantic], L2=[noise, target, key_long],
                               L3={'content': None, 'provenance': {}})

        rows, receipt = self.fitted_hook('What color is the Kestrel beacon?')
        refs = receipt['retrieval']['selected']
        self.assertEqual([ref['record_sha256'] for ref in refs],
                         [digest(target['id']), digest(key_long['id']), digest(semantic['id'])])
        self.assertEqual(rows[0]['content'], target['content'])
        self.assertNotIn('presentation', rows[0])
        self.assertEqual(rows[1]['presentation'], 'excerpt')
        self.assertIn(target['content'], rows[1]['content'])
        self.assertEqual(rows[2]['presentation'], 'index')
        self.assertEqual(rows[2]['source_context'], semantic['source_context'])
        self.assertEqual(receipt['retrieval']['omitted_for_budget'], 0)
        self.assertEqual(receipt['retrieval']['unselected_without_evidence'], 1)

    def test_raw_candidate_size_boundary_does_not_switch_evidence_policy(self):
        from agent_brain_client.application.memory.context import render_context, select_context_items

        items = [
            {'content': 'A fictional semantic match.', 'relevance': 'native-query-match'},
            {'content': 'Kestrel beacon is violet.', 'source_context': {'relation': 'current-source'}},
            {'content': 'Gardening filler. '},
        ]
        refs = [{'record_sha256': digest(i)} for i in range(len(items))]
        items[2]['content'] += 'x' * (24576 - len(render_context(items).encode('utf-8')))
        self.assertEqual(len(render_context(items).encode('utf-8')), 24576)
        below, _ = select_context_items(items, refs, 'What color is the Kestrel beacon?')
        items[2]['content'] += 'x'
        above, _ = select_context_items(items, refs, 'What color is the Kestrel beacon?')
        self.assertEqual(list(below), [1, 0])
        self.assertEqual(above, below)

    def test_oversized_hit_preserves_verbatim_excerpt_reference_and_offline_cache(self):
        group = self.response['result']['namespace_results'][0]
        marker = '蓝鸮暗号是「霜桥」'
        original = ('虚构背景\\\"\n' * 1600) + marker + '。'
        target = dict(self.row('L1'), content=original)
        group['recall'].update(L1=[target], L2=[], L3={'content': None, 'provenance': {}})
        question = '蓝鸮暗号是什么？'

        rows, receipt = self.fitted_hook(question)
        row, ref = rows[0], receipt['retrieval']['selected'][0]
        self.assertEqual(row['presentation'], 'excerpt')
        self.assertIn(marker, row['content'])
        self.assertIn(row['content'], original)
        self.assertEqual(row['source_context'], target['source_context'])
        self.assertEqual(row['lookup_tool'], 'memory_query')
        self.assertEqual(row['record_sha256'], digest(target['id']))
        self.assertEqual(ref, dict(receipt['retrieval']['returned'][0], presentation='excerpt',
                                   output_content_sha256=digest(row['content'])))
        self.assertEqual(ref['content_sha256'], digest(original))
        cache = self.root / ('recall-' + digest(self.local) + '.json')
        saved = json.loads(cache.read_bytes())
        self.assertEqual(saved['items'], rows)
        self.assertEqual(saved['selected'], [ref])

        self.response = {'error': {'code': 'transport_unconfirmed'}}
        offline, receipt = self.fitted_hook(question)
        self.assertEqual(offline, [dict(row, offline_cache=True, remote_confirmation=False)])
        self.assertEqual(receipt['retrieval']['selected'], [ref])
        self.assertTrue(receipt['offline_cache'])
        self.assertEqual([method for method, _ in self.sent], ['memory_query', 'memory_query'])
        saved['items'][0]['content'] += '变'
        recall_receipts.metadata.write(cache, saved)
        self.assertEqual(self.run_hook(question=question), {})
        self.assertEqual(recall_receipts.recall_observation(self.value)['status'], 'failed')

    def test_budget_omits_uncorroborated_noise_and_indexes_relevant_overflow(self):
        group = self.response['result']['namespace_results'][0]
        noise = [
            dict(self.row('L2'), id=f'synthetic-noise-{i}', content='Kestrel gardening. ' + '花' * 800)
            for i in range(21)
        ]
        evidence = [
            dict(self.row('L2'), id=f'synthetic-beacon-{i}', content=f'Kestrel beacon {i} ' + '界' * 800)
            for i in range(20)
        ]
        group['recall'].update(L1=[], L2=noise + evidence, L3={'content': None, 'provenance': {}})
        rows, receipt = self.fitted_hook('What about the Kestrel beacon?')
        evidence_by_ref = {digest(row['id']): row for row in evidence}
        retrieval = receipt['retrieval']
        self.assertEqual(retrieval['candidate_count'], 41)
        self.assertEqual(retrieval['omitted_for_budget'], len(evidence) - len(rows))
        self.assertEqual(retrieval['unselected_without_evidence'], len(noise))
        self.assertEqual(len(retrieval['selected']), len(rows))
        self.assertIn('index', {row.get('presentation') for row in rows})
        for row, ref in zip(rows, retrieval['selected']):
            original = evidence_by_ref[ref['record_sha256']]
            self.assertIn(row['content'], original['content'])
            self.assertEqual(row['source_context'], original['source_context'])
            self.assertEqual(ref.get('output_content_sha256', ref['content_sha256']), digest(row['content']))

    def test_native_semantic_match_without_keyword_keeps_verified_source_on_overflow(self):
        group = self.response['result']['namespace_results'][0]
        semantic = dict(self.row('L1'), content='Leaving before sunrise avoids rush hour.')
        oversized = dict(self.row('L2'), content='山' * 9000)
        group['recall'].update(L1=[semantic], L2=[oversized], L3={'content': None, 'provenance': {}})
        rows, receipt = self.fitted_hook('Which departure is calmer?')
        self.assertEqual([row['content'] for row in rows], [semantic['content']])
        self.assertEqual(rows[0]['source_context'], semantic['source_context'])
        self.assertEqual(rows[0]['relevance'], 'native-query-match')
        self.assertEqual(receipt['retrieval']['selected'], receipt['retrieval']['returned'][:1])

    def test_offline_legacy_full_rows_get_same_policy_under_existing_approval(self):
        group = self.response['result']['namespace_results'][0]
        target = dict(self.row('L2'), content='Kestrel beacon glows violet.',
                      source_context=source_context([self.local], self.local))
        noise = dict(self.row('L2'), id='synthetic-unrelated', content='Gardening notes.')
        group['recall'].update(L1=[], L2=[noise, target], L3={'content': None, 'provenance': {}})
        question = 'What color is the Kestrel beacon?'
        manual = gm.recall(self.value, self.local, 'synthetic-reader', question)
        self.assertEqual([row['content'] for row in manual], [noise['content'], target['content']])
        cache = self.root / ('recall-' + digest(self.local) + '.json')
        saved = json.loads(cache.read_bytes())
        saved.pop('evidence_selected')
        recall_receipts.metadata.write(cache, saved)
        self.response = {'error': {'code': 'transport_unconfirmed'}}
        rows, receipt = self.fitted_hook(question)
        self.assertEqual(rows, [dict(manual[1], offline_cache=True, remote_confirmation=False)])
        self.assertEqual(receipt['retrieval']['unselected_without_evidence'], 1)
        self.assertEqual(receipt['retrieval']['selected'][0]['record_sha256'], digest(target['id']))
        with patch.object(gm, 'approved', return_value=False):
            self.assertEqual(self.run_hook(question=question), {})

    def test_request_receipt_survives_later_query_and_failed_query_has_receipt(self):
        self.run_hook()
        first = recall_receipts.recall_observation(self.value)
        self.assertFalse(first['output_written'])
        self.assertEqual(first['query_sha256'], digest('Synthetic question marker'))
        self.assertEqual(first['retrieval']['selected'][0]['content_sha256'], digest('Synthetic L1 answer marker'))
        self.assertEqual(first['retrieval']['selected'][0]['record_sha256'], digest('synthetic-L1'))
        original = self.prompt
        def next_prompt():
            data = json.loads(original().buffer.read()); data['turn_id'] += '-next'
            return SimpleNamespace(buffer=io.BytesIO(json.dumps(data).encode()))
        with patch.object(self, 'prompt', side_effect=next_prompt), patch.object(gm, 'recall', side_effect=ValueError('FICTIONAL_PRIVATE_ERROR')):
            self.assertEqual(self.run_hook(), {})
        failed = recall_receipts.recall_observation(self.value)
        self.assertEqual(failed['status'], 'failed'); self.assertEqual(failed['error_type'], 'ValueError')
        self.assertNotEqual(first['request_key'], failed['request_key'])
        self.assertEqual(recall_receipts.recall_observation(self.value, first['request_key']), first)
        self.assertNotIn('FICTIONAL_PRIVATE_ERROR', json.dumps(failed))

    def test_cli_observes_stdout_and_redacts_failure(self):
        output, diagnostic = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['global_memory', 'global-hook']), patch.object(gm, 'load', return_value=self.value), patch.object(gm, 'eligible', return_value=self.local), patch.object(gm, 'submit_hook', return_value={'status': 'staged'}), patch.object(sys, 'stdin', self.prompt()), patch.object(sys, 'stdout', output), patch.object(sys, 'stderr', diagnostic):
            self.assertEqual(collection_cli.main(), 0)
        self.assertIn('additionalContext', output.getvalue())
        entries = [json.loads(line) for line in diagnostic.getvalue().splitlines()]
        receipt = next(row for row in entries if row.get('status') == 'hook-output-written')
        self.assertEqual(receipt['status'], 'hook-output-written'); self.assertEqual(receipt['recall_status'], 'context-prepared')
        self.assertTrue(recall_receipts.recall_observation(self.value, receipt['request_key'])['output_written'])
        self.assertNotIn('answer marker', diagnostic.getvalue()); self.assertEqual(receipt['model_used'], 'unknown')
        with patch.object(sys, 'argv', ['global_memory', 'global-hook']), patch.object(gm, 'load', side_effect=RuntimeError('SYNTHETIC_SECRET')), patch.object(sys, 'stdout', io.StringIO()), patch.object(sys, 'stderr', io.StringIO()) as errors:
            self.assertEqual(collection_cli.main(), 0)
        self.assertNotIn('SYNTHETIC_SECRET', errors.getvalue()); self.assertNotIn('hook-output-written', errors.getvalue())

    def test_cli_failed_recall_never_claims_context_was_output(self):
        output, diagnostic = io.StringIO(), io.StringIO()
        with (patch.object(sys, 'argv', ['global_memory', 'global-hook']),
              patch.object(gm, 'load', return_value=self.value),
              patch.object(gm, 'eligible', return_value=self.local),
              patch.object(gm, 'submit_hook', return_value={'status': 'staged'}),
              patch.object(gm, 'recall', side_effect=ValueError('FICTIONAL_PRIVATE_ERROR')),
              patch.object(sys, 'stdin', self.prompt()),
              patch.object(sys, 'stdout', output), patch.object(sys, 'stderr', diagnostic)):
            self.assertEqual(collection_cli.main(), 0)
        self.assertEqual(json.loads(output.getvalue()), {})
        receipt = recall_receipts.recall_observation(self.value)
        self.assertEqual(receipt['status'], 'failed')
        self.assertFalse(receipt['output_written'])
        self.assertNotIn('hook-output-written', diagnostic.getvalue())
        self.assertNotIn('FICTIONAL_PRIVATE_ERROR', diagnostic.getvalue())

    def test_opt_in_codex_failure_timing_is_bounded_and_matches_exact_receipt(self):
        from agent_brain_client.application import control

        clock = SimpleNamespace(now=0, monotonic=lambda: clock.now)
        write = recall_receipts.metadata.write

        def recall(*args, **kwargs):
            clock.now = 20
            with control.recall_response_headers():
                for _ in range(12):
                    with control.recall_header_read(90):
                        clock.now += 1
            raise ValueError('FICTIONAL_PRIVATE_ERROR')

        def timed_write(path, value, **kwargs):
            clock.now += 0.125
            return write(path, value, **kwargs)

        with (patch.dict(os.environ, {control.RECALL_TIMING_ENV: '1'}),
              patch.object(control, 'time', clock),
              patch.object(collection_cli, 'time', clock),
              patch.object(recall_receipts.metadata, 'write', side_effect=timed_write),
              patch.object(gm, 'load', return_value=self.value),
              patch.object(gm, 'eligible', return_value=self.local),
              patch.object(gm, 'submit_hook', return_value={'status': 'staged'}),
              patch.object(gm, 'recall', side_effect=recall),
              patch.object(sys, 'stdin', self.prompt()),
              patch.object(sys, 'stdout', io.StringIO()) as output,
              patch.object(sys, 'stderr', io.StringIO())):
            self.assertEqual(collection_cli.main(['global-hook']), 0)
        self.assertEqual(json.loads(output.getvalue()), {})
        sample_text = (self.root / 'last-recall-timing.json').read_text()
        sample = json.loads(sample_text)
        receipt = recall_receipts.recall_observation(self.value, sample['request_key'])
        exact = self.root / 'recall-receipts' / (receipt['request_key'] + '.json')
        self.assertEqual(sample['receipt_sha256'], recall_receipts.metadata.digest(exact.read_bytes()))
        self.assertEqual(sample['first_read_budget_ms'], {'request_ms': 90000, 'socket_ms': 90000})
        reads = sample['response_header_reads']
        self.assertEqual(reads['count'], 12)
        self.assertEqual(len(reads['first_spans_ms']), 8)
        self.assertEqual(reads['first_spans_ms'][0], [20000, 21000])
        self.assertEqual(reads['last_span_ms'], [31000, 32000])
        self.assertEqual(sample['failure_receipt_write_ms'], 250)
        self.assertEqual(receipt['error_type'], 'ValueError')
        self.assertFalse(receipt['output_written'])
        self.assertNotIn('FICTIONAL_PRIVATE_ERROR', sample_text)
        self.assertNotIn('/synthetic/', sample_text)

    def test_opt_in_codex_success_timing_includes_stdout_flush_and_exact_receipt(self):
        from agent_brain_client.application import control

        clock = SimpleNamespace(now=0, monotonic=lambda: clock.now)
        original_recall = gm.recall

        def recall(*args, **kwargs):
            with control.recall_timing_stage('response_headers'):
                clock.now = 0.2
            return original_recall(*args, **kwargs)

        class Output(io.StringIO):
            def flush(self):
                self_test.assertFalse((self_test.root / 'last-recall-timing.json').exists())
                clock.now = 0.3
                super().flush()

        self_test = self
        output = Output()
        with (patch.dict(os.environ, {control.RECALL_TIMING_ENV: '1'}),
              patch.object(control, 'time', clock),
              patch.object(collection_cli, 'time', clock),
              patch.object(gm, 'load', return_value=self.value),
              patch.object(gm, 'eligible', return_value=self.local),
              patch.object(gm, 'submit_hook', return_value={'status': 'staged'}),
              patch.object(gm, 'recall', side_effect=recall),
              patch.object(sys, 'stdin', self.prompt()),
              patch.object(sys, 'stdout', output),
              patch.object(sys, 'stderr', io.StringIO())):
            self.assertEqual(collection_cli.main(['global-hook']), 0)
        self.assertIn('additionalContext', json.loads(output.getvalue())['hookSpecificOutput'])
        sample_text = (self.root / 'last-recall-timing.json').read_text()
        sample = json.loads(sample_text)
        offsets = sample['phase_offsets_ms']
        self.assertEqual(offsets['response_headers'], [0, 200])
        self.assertEqual(offsets['output_prepared'], 200)
        self.assertEqual(offsets['output_flushed'], 300)
        self.assertNotIn('failed', offsets)
        receipt = recall_receipts.recall_observation(self.value, sample['request_key'])
        exact = self.root / 'recall-receipts' / (receipt['request_key'] + '.json')
        self.assertTrue(receipt['output_written'])
        self.assertEqual(sample['receipt_sha256'], recall_receipts.metadata.digest(exact.read_bytes()))
        self.assertNotIn('Synthetic L1 answer marker', sample_text)

    def test_stdout_write_and_flush_interruption_allow_same_turn_recall_retry(self):
        class Interrupted(BaseException):
            pass

        for boundary in ('write', 'flush'):
            with self.subTest(boundary=boundary):
                output = io.StringIO()
                original_prompt = self.prompt
                def prompt():
                    payload = json.loads(original_prompt().buffer.read())
                    payload['turn_id'] += '-' + boundary
                    return SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))
                with (patch.object(sys, 'argv', ['global_memory', 'global-hook']),
                      patch.object(gm, 'load', return_value=self.value),
                      patch.object(gm, 'eligible', return_value=self.local),
                      patch.object(gm, 'submit_hook', return_value={'status': 'staged'}),
                      patch.object(sys, 'stdin', prompt()),
                      patch.object(sys, 'stdout', output),
                      patch.object(sys, 'stderr', io.StringIO()),
                      patch.object(output, boundary, side_effect=Interrupted)):
                    with self.assertRaises(Interrupted):
                        collection_cli.main()
                receipt = recall_receipts.recall_observation(self.value)
                self.assertFalse(receipt['output_written'])
                output = io.StringIO()
                with (patch.object(sys, 'argv', ['global_memory', 'global-hook']),
                      patch.object(gm, 'load', return_value=self.value),
                      patch.object(gm, 'eligible', return_value=self.local),
                      patch.object(gm, 'submit_hook', return_value={'status': 'staged'}),
                      patch.object(sys, 'stdin', prompt()),
                      patch.object(sys, 'stdout', output),
                      patch.object(sys, 'stderr', io.StringIO())):
                    self.assertEqual(collection_cli.main(), 0)
                self.assertIn('additionalContext', output.getvalue())
                self.assertTrue(recall_receipts.recall_observation(self.value)['output_written'])


if __name__ == '__main__': unittest.main()
