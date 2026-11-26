import agent_brain_client.interfaces.cli.collection as collection_cli
import agent_brain_client.adapters.storage.recall_receipts as recall_receipts
"""Synthetic shared Mac/Windows recall, with no credentials or native sessions."""
import copy
import hashlib
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
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

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
                                 params=lambda session, *, recall: {'origin': self.local, 'session_id': session},
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

    def test_explicit_windows_origin_reaches_transport_in_existing_wire_form(self):
        from agent_brain_client.adapters.server.https import HTTPSContentClient

        self.local = self.foreign = self.origin(r'C:\Synthetic\Project')
        extended = self.origin(r'\\?\C:\Synthetic\Project')
        params = dict(origin=extended, session_id='synthetic-reader',
                      query='synthetic question', limit=3)
        request = dict(request_id='synthetic-query', method='memory_query', params=params)
        result = {'schema': 1, 'namespace_results': [self.group()]}
        client = HTTPSContentClient.__new__(HTTPSContentClient)
        client.device_task = client.recall_policy = None
        client.grant = {'scopes': ['read'], 'artifact_types': ['memory']}
        client.device_id = self.device
        client.transport = Mock()
        client.transport.request.return_value = {'request_id': request['request_id'], 'result': result}
        self.assertEqual(client.recall(request)['result'], result)
        client.transport.request.assert_called_once_with(
            dict(request, params=dict(params, origin=self.foreign))
        )
        self.assertEqual(request['params']['origin']['cwd'], r'\\?\C:\Synthetic\Project')

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
        first = self.group()
        self.foreign = self.origin('/synthetic/another-source')
        self.response['result']['namespace_results'] = [first, self.group()]
        result=self.run_hook()
        self.assertTrue(result)
        receipt=recall_receipts.recall_observation(self.value)
        self.assertEqual(receipt['item_count'],6)
        self.assertEqual(len(receipt['retrieval']['selected']),6)

    def test_duplicate_text_with_same_applicability_uses_the_native_match_once(self):
        group = self.response['result']['namespace_results'][0]
        native = group['recall']['L1'][0]
        other = self.origin('/synthetic/another-source')
        derived = self.group()
        derived['recall'].update(L1=[], L3={'content': None, 'provenance': {}})
        derived['recall']['L2'][0]['content'] = native['content']
        self.response['result']['namespace_results'].insert(0, derived)
        group['recall']['L2'] = []
        group['recall']['L3'].update(content=native['content'],
                                    source_context=source_context([other], self.local))

        rows, receipt = self.fitted_hook('Fictional question with no literal match')
        self.assertEqual([row['layer'] for row in rows], ['L1', 'L3'])
        self.assertEqual([row['content'] for row in rows], [native['content']] * 2)
        self.assertEqual(rows[0]['source_context'], native['source_context'])
        self.assertEqual(rows[1]['source_context']['origins'], [other])
        retrieval = receipt['retrieval']
        self.assertEqual(retrieval['candidate_count'], 3)
        self.assertEqual(retrieval['unselected_without_evidence'], 1)
        self.assertEqual(retrieval['omitted_for_budget'], 0)
        self.assertEqual(retrieval['selected'][0], retrieval['returned'][1])

    def test_empty_peer_groups_preserve_native_per_namespace_results(self):
        empty = self.group(); empty['recall'].update(L1=[], L2=[], L3={'content': None, 'provenance': {}})
        self.response['result']['namespace_results'] = [empty, empty, self.group()]
        self.assertEqual(len(self.read()), 3)
        self.response['result']['namespace_results'] = [self.group(), self.group()]
        self.assertEqual(len(self.read()), 6)
        self.response['result']['namespace_results'] = [empty] * 19
        with self.assertRaises(ValueError): self.read()

    def test_bad_source_does_not_erase_an_independently_verified_source(self):
        valid, invalid = self.group(), self.group()
        self.response['result']['namespace_results'] = [valid]
        verified_output = self.run_hook()
        cache, = self.root.glob('recall-*.json')
        complete_snapshot = cache.read_bytes()
        invalid['source_device_id'] = 'd' * 32
        for layer in ('L1', 'L2'):
            item = invalid['recall'][layer][0]
            item.update(id='rejected-' + layer, content='Unverified source fixture')
            item['provenance']['sources'][0]['identity']['device_id'] = 'd' * 32
        proof = invalid['recall']['L3']['provenance']
        for sources in ([None], [dict(proof['sources'][0], identity=None)], proof['sources']):
            proof['sources'] = sources
            for groups in ([invalid, valid], [valid, invalid]):
                with self.subTest(sources=sources, bad_first=groups[0] is invalid):
                    self.response['result']['namespace_results'] = groups
                    result = self.run_hook()
                    self.assertEqual(result, verified_output)
                    context = result['hookSpecificOutput']['additionalContext']
                    for layer in ('L1', 'L2', 'L3'):
                        self.assertIn('Synthetic ' + layer + ' answer marker', context)
                    self.assertNotIn('Unverified source fixture', context)
                    receipt = recall_receipts.recall_observation(self.value)
                    self.assertEqual(receipt['item_count'], 3)
                    evidence = receipt['retrieval']
                    self.assertIs(evidence['partial'], True)
                    self.assertIs(evidence['source_verification_failed'], True)
                    self.assertNotIn('server_budget_trimmed', evidence)
                    for key in ('returned', 'selected'):
                        self.assertEqual([ref['record_sha256'] for ref in evidence[key]],
                                         [digest('synthetic-' + layer) for layer in ('L1', 'L2', 'L3')])
                        self.assertNotIn(digest('Unverified source fixture'),
                                         [ref['content_sha256'] for ref in evidence[key]])
        self.assertEqual(cache.read_bytes(), complete_snapshot)

        self.response['recall_partial'] = True
        self.assertEqual(self.run_hook(), verified_output)
        receipt = recall_receipts.recall_observation(self.value)
        self.assertIs(receipt['retrieval']['server_budget_trimmed'], True)
        self.assertIs(receipt['retrieval']['source_verification_failed'], True)
        self.assertEqual(cache.read_bytes(), complete_snapshot)

        # Old partial receipts had only the aggregate flag.
        receipt['retrieval'].pop('server_budget_trimmed')
        receipt['retrieval'].pop('source_verification_failed')
        recall_receipts.save_recall_observation(self.value, receipt)
        self.assertEqual(recall_receipts.recall_observation(self.value), receipt)

        self.response['result']['namespace_results'] = [invalid]
        self.assertEqual(self.run_hook(), {})
        receipt = recall_receipts.recall_observation(self.value)
        self.assertEqual(receipt['status'], 'failed')
        self.assertIs(receipt['retrieval']['server_budget_trimmed'], True)
        self.assertIs(receipt['retrieval']['source_verification_failed'], True)
        self.assertEqual(cache.read_bytes(), complete_snapshot)

    def test_hook_reads_budget_marker_only_from_a_successful_matching_envelope(self):
        from agent_brain_client.adapters.server.collection import CollectionClient

        wire_client = object.__new__(CollectionClient)
        opener = Mock()
        wire_client.base = SimpleNamespace(
            _credential=lambda: 'fictional-token',
            profile={'endpoint': 'https://fixture.invalid'},
            transport=SimpleNamespace(opener=opener),
        )

        def reply(request, **kwargs):
            request_id = json.loads(request.data)['request_id']
            return io.BytesIO(json.dumps(dict(request_id=request_id, **self.response)).encode())

        opener.open.side_effect = reply
        with patch.object(gm.CollectionClient.return_value, 'wire', wire_client.wire):
            expected = self.run_hook()
            self.assertNotIn('partial', recall_receipts.recall_observation(self.value)['retrieval'])
            cache = self.root / ('recall-' + digest(self.local) + '.json')
            cached = cache.read_bytes()
            for marker in (False, None, 1, 'true', [{}], True):
                with self.subTest(marker=marker):
                    self.response['recall_partial'] = marker
                    self.assertEqual(self.run_hook(), expected)
                    retrieval = recall_receipts.recall_observation(self.value)['retrieval']
                    self.assertEqual('partial' in retrieval, marker is True)
                    self.assertEqual('server_budget_trimmed' in retrieval, marker is True)
                    self.assertNotIn('source_verification_failed', retrieval)
                    self.assertEqual(cache.read_bytes(), cached)

            self.response['result']['recall_partial'] = True
            self.assertEqual(self.run_hook(), {})
            receipt = recall_receipts.recall_observation(self.value)
            self.assertEqual(receipt['status'], 'failed')
            self.assertNotIn('partial', receipt['retrieval'])
            del self.response['result']['recall_partial']

            opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(
                json.dumps(dict(self.response, request_id='unmatched-fixture')).encode()
            )
            self.assertEqual(self.run_hook(), {})
            receipt = recall_receipts.recall_observation(self.value)
            self.assertEqual(receipt['status'], 'failed')
            self.assertNotIn('partial', receipt['retrieval'])

            del self.response['result']
            self.response['error'] = {'code': 'transport_unconfirmed', 'retryable': True}
            opener.open.side_effect = reply
            offline = self.run_hook()
            self.assertIn('Synthetic L1 answer marker', offline['hookSpecificOutput']['additionalContext'])
            receipt = recall_receipts.recall_observation(self.value)
            self.assertIs(receipt['offline_cache'], True)
            self.assertNotIn('partial', receipt['retrieval'])

    def test_hook_budget_only_empty_result_is_observable(self):
        self.response['result']['namespace_results'] = []
        self.response['recall_partial'] = True
        self.assertEqual(self.run_hook(), {})
        receipt = recall_receipts.recall_observation(self.value)
        self.assertEqual(receipt['status'], 'empty')
        self.assertIs(receipt['retrieval']['partial'], True)
        self.assertIs(receipt['retrieval']['server_budget_trimmed'], True)
        self.assertNotIn('source_verification_failed', receipt['retrieval'])

    def test_server_budget_trim_preserves_the_last_complete_offline_snapshot(self):
        cache = self.root / ('recall-' + digest(self.local) + '.json')
        complete = copy.deepcopy(self.response)
        partial = dict(copy.deepcopy(complete), recall_partial=True)
        partial['result']['namespace_results'][0]['recall']['L1'][0]['content'] = 'Synthetic newer answer marker'
        unavailable = {'error': {'code': 'transport_unconfirmed', 'retryable': True}}

        self.response = partial
        output = self.run_hook()
        self.assertIn('Synthetic newer answer marker', output['hookSpecificOutput']['additionalContext'])
        self.assertFalse(cache.exists())
        self.response = unavailable
        self.assertEqual(self.run_hook(), {})
        self.assertEqual(recall_receipts.recall_observation(self.value)['status'], 'failed')

        self.response = complete
        self.run_hook()
        snapshot = cache.read_bytes()
        self.response = partial
        self.assertEqual(self.run_hook(), output)
        receipt = recall_receipts.recall_observation(self.value)
        self.assertIs(receipt['retrieval']['server_budget_trimmed'], True)
        self.assertEqual(cache.read_bytes(), snapshot)

        self.response = unavailable
        offline = self.run_hook()['hookSpecificOutput']['additionalContext']
        self.assertIn('Synthetic L1 answer marker', offline)
        self.assertNotIn('Synthetic newer answer marker', offline)
        receipt = recall_receipts.recall_observation(self.value)
        self.assertIs(receipt['offline_cache'], True)
        self.assertNotIn('partial', receipt['retrieval'])

    def test_old_cache_is_only_read_on_retryable_transport_failure(self):
        from agent_brain_client.adapters.server.collection import CollectionClient

        self.assertEqual(len(self.read()), 3)
        read_cache = recall_receipts.metadata.private_json
        wire_client = object.__new__(CollectionClient)
        opener = Mock()
        wire_client.base = SimpleNamespace(
            _credential=lambda: 'fictional-token',
            profile={'endpoint': 'https://fixture.invalid'},
            transport=SimpleNamespace(opener=opener),
        )
        with (patch.object(gm.CollectionClient.return_value, 'wire', wire_client.wire),
              patch.object(recall_receipts.metadata, 'private_json', wraps=read_cache) as read):
            for code in ('global_source_device_denied', 'transport_unconfirmed'):
                body = json.dumps({'error': {'code': code}}).encode()
                opener.open.side_effect = HTTPError('https://fixture.invalid', 403, 'denied', None, io.BytesIO(body))
                with self.assertRaisesRegex(ValueError, 'Global recall unavailable'):
                    self.read()
            read.assert_not_called()
            opener.open.side_effect = URLError('Fictional transport failure')
            items = self.read()
            read.assert_called_once()
        self.assertEqual(len(items), 3)
        self.assertTrue(all(row['offline_cache'] and not row['remote_confirmation'] for row in items))

    def prompt(self, question='Synthetic question marker'):
        payload = {'cwd': self.local['cwd'], 'hook_event_name': 'UserPromptSubmit',
                   'session_id': 'synthetic-reader', 'turn_id': 'synthetic-turn', 'prompt': question}
        return SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))

    def run_hook(self, *, question=None, **kwargs):
        prompt = self.prompt() if question is None else self.prompt(question)
        with patch.object(gm, 'eligible', return_value=self.local), patch.object(gm, 'submit_hook', return_value={'status': 'staged'}), patch.object(sys, 'stdin', prompt):
            return gm.hook(self.value, **kwargs)

    def final_result(self, body):
        row = self.row("L1")
        sources = [{
            "layer": "L1", "project_id": self.project, "sharing": "personal",
            "source_device_id": self.peer, "source_context": row["source_context"],
            "provenance": row["provenance"], "record_sha256": digest(row["id"]),
            "content_sha256": digest(row["content"]),
        }] if body else []
        return {"schema": 2, "final_context": {
            "format": "agentbrain.final-context.v1", "body": body,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "sources": sources,
        }}

    def test_both_hooks_inject_negotiated_body_and_preserve_receipts(self):
        from agent_brain_client.adapters.harnesses.codebuddy import events, transcript
        from agent_brain_client.domain.final_context import HOST_BYTES

        workspace = self.root / "synthetic-workspace"
        workspace.mkdir()
        self.local = self.origin(str(workspace))
        self.value["scope"]["include_roots"] = [{"root": str(workspace), "sharing": "personal"}]
        pin = digest(self.value["scope"])
        self.value.update(approved_scope_sha256=pin, server_scope_sha256=pin)
        body = "  [B] synthetic cobalt-28  \n[A] synthetic amber-64\n  项目 B 的证据\n"
        self.response["result"] = self.final_result(body)
        self.response["persistence"] = {"state": "read-only", "durable": False}
        client = gm.CollectionClient.return_value
        client.capabilities = {"final-context-v1", "project-origin-v1"}
        client.recall_workspace_project = lambda: {
            "repository": "github.com/synthetic/project-a",
            "id": "git-sha256:5dab25d7ae2666ed57940bd42bcfb28737306db37df66f98b5ccd4d204f537ec",
        }
        for harness in ("codex", "codebuddy"):
            with (
                self.subTest(harness=harness),
                patch.object(gm, "load", return_value=self.value),
                patch.object(os, "getcwd", return_value=self.local["cwd"]),
                patch.object(events, "capture_messages", return_value={"status": "staged"}),
                patch.object(transcript, "observe", return_value="synthetic"),
                patch("agent_brain_client.application.memory.recall._fit_context",
                      side_effect=AssertionError("Finished body must bypass legacy fitting")),
            ):
                output = self.run_hook(harness=harness)
                self.assertEqual(output["hookSpecificOutput"]["additionalContext"].encode(), body.encode())
                method, params = self.sent[-1]
                self.assertEqual(method, "memory_query")
                self.assertEqual(params["final_context"], {
                    "format": "agentbrain.final-context.v1", "host": harness,
                    "max_utf8_bytes": HOST_BYTES[harness],
                })
                self.assertEqual(params["origin"]["project"], client.recall_workspace_project())
                receipt = recall_receipts.recall_observation(self.value)
                self.assertEqual(receipt["context_sha256"], digest(body))
                self.assertEqual(receipt["context_bytes"], len(body.encode()))
                self.assertEqual(receipt["layer_counts"], {"L1": 1, "L2": 0, "L3": 0})

    def test_negotiated_recall_rejects_incompatible_or_unverified_body(self):
        client = gm.CollectionClient.return_value
        client.capabilities = {"final-context-v1"}
        self.response["persistence"] = {"state": "read-only", "durable": False}
        legacy = self.response["result"]
        bad_format = self.final_result("Synthetic recognized evidence")
        bad_format["final_context"]["format"] = "agentbrain.final-context.v9"
        bad_proof = self.final_result("Synthetic recognized evidence")
        bad_proof["final_context"]["sources"][0]["provenance"]["sources"][0]["state"] = "pending"
        bad_hash = self.final_result("Synthetic recognized evidence")
        bad_hash["final_context"]["body"] += " changed"
        for result in (legacy, bad_format, bad_proof, bad_hash, self.final_result("证" * 4000)):
            self.response["result"] = result
            self.assertEqual(self.run_hook(), {})
            self.assertEqual(recall_receipts.recall_observation(self.value)["status"], "failed")
        self.response["result"] = self.final_result("")
        self.assertEqual(self.run_hook(), {})
        self.assertEqual(recall_receipts.recall_observation(self.value)["status"], "empty")
        self.response["persistence"]["durable"] = True
        self.assertEqual(self.run_hook(), {})
        self.assertEqual(recall_receipts.recall_observation(self.value)["status"], "failed")

    def test_replays_server_final_context_contract_fixture(self):
        result = json.loads((ROOT / "tests/fixtures/server_final_context.json").read_text(encoding="utf-8"))
        self.response.update(result=result, persistence={"state": "read-only", "durable": False})
        self.local = self.origin("/synthetic/wt-b")
        self.value["scope"].update(all_projects=True, profile_id="a" * 32)
        client = gm.CollectionClient.return_value
        client.actor, client.device = "owner", "a" * 32
        client.capabilities = {"final-context-v1"}
        output = self.run_hook(question="Project B release gate")
        final = result["final_context"]
        body = output["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), final["body_sha256"])
        self.assertEqual(body, final["body"])
        receipt = recall_receipts.recall_observation(self.value)
        self.assertEqual(receipt["retrieval"]["selected"][0]["record_sha256"],
                         final["sources"][0]["record_sha256"])
        self.assertEqual(receipt["retrieval"]["final_context_format"], final["format"])

    def test_both_native_hooks_route_projects_and_ordinary_directories(self):
        from agent_brain_client.adapters.harnesses.codebuddy import events, transcript
        from agent_brain_client.adapters.server.collection import CollectionClient

        directories = {name: self.root / name for name in ("project", "ordinary")}
        for directory in directories.values():
            directory.mkdir()
        (directories["project"] / ".git").mkdir()
        base_scope = dict(self.value["scope"], all_projects=True, include_roots=[])
        for harness in ("codex", "codebuddy"):
            for kind, scope, capture, recall, question in (
                ("project", dict(base_scope, all_non_projects=True), True, True, "Synthetic project question"),
                ("ordinary", dict(base_scope, all_non_projects=True), True, True, "Synthetic ordinary question"),
                ("ordinary", base_scope, False, True, "Synthetic prior default question"),
                ("ordinary", dict(base_scope, all_non_projects=False), False, False, "Synthetic narrow scope"),
                ("ordinary", dict(base_scope, recall_personal=False), False, False, "Synthetic personal recall off"),
                ("ordinary", dict(base_scope, all_non_projects=True, recall_personal=False),
                 True, False, "Synthetic capture with recall off"),
                ("ordinary", dict(base_scope, all_non_projects=True,
                                  include_roots=[{"root": str(directories["ordinary"]), "sharing": "project"}]),
                 False, False, "Synthetic isolated directory with no marker"),
                ("ordinary", dict(base_scope, all_non_projects=True, exclude_roots=[str(directories["ordinary"])]),
                 False, False, "Synthetic excluded directory"),
                ("ordinary", dict(base_scope, all_non_projects=True), True, False, "[LocalOnly] Synthetic private prompt"),
            ):
                with self.subTest(harness=harness, kind=kind, question=question):
                    directory = directories[kind]
                    self.local = gm.discover(str(directory))
                    self.response["result"]["namespace_results"] = [self.group()]
                    self.response["recall_partial"] = kind == "project"
                    pin = digest(scope)
                    value = dict(self.value, scope=scope, approved_scope_sha256=pin, server_scope_sha256=pin)
                    client = CollectionClient.__new__(CollectionClient)
                    client.value, client.local = value, self.local
                    client.actor, client.device = "synthetic-owner", self.device
                    client.wire = lambda method, params, **kwargs: self.send(method, params)
                    payload = {
                        "cwd": str(directory), "hook_event_name": "UserPromptSubmit",
                        "session_id": "synthetic-reader", "turn_id": "synthetic-turn",
                        "generation_id": "synthetic-generation", "prompt": question,
                    }
                    self.sent.clear()
                    with (
                        patch.object(gm, "load", return_value=value),
                        patch.object(os, "getcwd", return_value=str(directory)),
                        patch.object(gm, "CollectionClient", return_value=client),
                        patch.object(gm, "schedule_delivery") as schedule,
                        patch.object(gm, "submit_hook", return_value={"status": "staged"}) as codex,
                        patch.object(events, "capture_messages", return_value={"status": "staged"}) as buddy,
                        patch.object(transcript, "observe", return_value="synthetic"),
                        patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))),
                        patch.object(sys, "stderr", io.StringIO()),
                    ):
                        output = gm.hook(value, harness=harness)
                    self.assertEqual(codex.call_count, int(capture and harness == "codex"))
                    self.assertEqual(buddy.call_count, int(capture and harness == "codebuddy"))
                    self.assertEqual(schedule.call_count, int(capture))
                    self.assertEqual(len(self.sent), 2 * int(recall))
                    if recall:
                        self.assertEqual(self.sent[0], ("memory_global_status", {}))
                        method, params = self.sent[1]
                        self.assertNotIn("final_context", params)
                        self.assertEqual(method, "memory_query")
                        self.assertEqual(params["origin"], self.local)
                        self.assertEqual(params["query"], question)
                        self.assertIn("Synthetic L1 answer marker", output["hookSpecificOutput"]["additionalContext"])
                        receipt = recall_receipts.recall_observation(value)
                        self.assertEqual("server_budget_trimmed" in receipt["retrieval"], kind == "project")
                        self.assertNotIn("server_budget_trimmed", json.dumps(output))
                    else:
                        self.assertEqual(output, {})

    def test_ordinary_recall_keeps_verified_personal_output_and_complete_cache(self):
        self.local = {"kind": "non-project", "cwd": "/synthetic/ordinary",
                      "project_root": None, "repository_root": None}
        scope = dict(self.value["scope"], all_projects=True, include_roots=[])
        pin = digest(scope)
        self.value.update(scope=scope, approved_scope_sha256=pin, server_scope_sha256=pin)
        personal = self.group()
        self.response["result"]["namespace_results"] = [personal]
        question = "Synthetic ordinary question"
        first = self.run_hook(question=question)
        cache = self.root / ("recall-" + digest(self.local) + ".json")
        complete = cache.read_bytes()
        project = copy.deepcopy(personal)
        project.update(sharing="project", project_id=namespace("synthetic-owner", "d" * 32))
        project["recall"]["L1"][0]["content"] = "Synthetic isolated content"
        self.response["result"]["namespace_results"] = [project, personal]
        mixed = self.run_hook(question=question)
        self.assertEqual(mixed, first)
        receipt = recall_receipts.recall_observation(self.value)
        self.assertTrue(receipt["retrieval"]["partial"])
        self.assertEqual(cache.read_bytes(), complete)
        self.response = {"error": {"code": "transport_unconfirmed", "retryable": True}}
        offline = self.run_hook(question=question)
        self.assertIn("Synthetic L1 answer marker", offline["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(recall_receipts.recall_observation(self.value)["offline_cache"])

    def test_codex_hook_recall_keeps_provenance_for_equivalent_windows_cwd(self):
        self.local = self.origin(r'C:\Synthetic\Project')
        self.foreign = self.local
        self.value['scope']['include_roots'] = [{'root': 'C:/Synthetic', 'sharing': 'personal'}]
        pin = digest(self.value['scope'])
        self.value.update(approved_scope_sha256=pin, server_scope_sha256=pin)
        self.response['result']['namespace_results'] = [self.group()]
        question = 'Synthetic question marker'
        for cwd, allowed in ((r'C:\Synthetic\Project-sibling', False),
                             (r'\\?\C:\Synthetic\Project\.', False),
                             (self.local['cwd'], True), (r'\\?\C:\Synthetic\Project', True)):
            with self.subTest(cwd=cwd):
                self.sent.clear()
                payload = dict(cwd=cwd, hook_event_name='UserPromptSubmit', session_id='synthetic-reader',
                               turn_id='synthetic-turn', prompt=question)
                stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))
                with (patch.object(gm, 'eligible', return_value=self.local),
                      patch.object(gm, 'submit_hook', return_value={'status': 'staged'}) as stage,
                      patch.object(sys, 'stdin', stdin)):
                    result = gm.hook(self.value)
                if allowed:
                    stage.assert_called_once_with(self.value, self.local, dict(payload, cwd=self.local['cwd']))
                    context = result['hookSpecificOutput']['additionalContext']
                    rows = [json.loads(line) for line in context.split('\n')[1:]]
                    self.assertCountEqual([row['layer'] for row in rows], ['L1', 'L2', 'L3'])
                    self.assertTrue(all(row['source_context']['relation'] == 'current-source' for row in rows))
                    self.assertEqual(len(self.sent), 1)
                    self.assertEqual(self.sent[0][1]['origin'], self.local)
                else:
                    self.assertEqual(result, {})
                    stage.assert_not_called()
                    self.assertEqual(self.sent, [])

    def fitted_hook(self, question, **kwargs):
        output = self.run_hook(question=question, **kwargs)
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
        self.assertNotIn('partial', receipt['retrieval'])
        self.assertEqual(receipt['status'], 'context-prepared'); self.assertEqual(receipt['model_used'], 'unknown')
        self.assertEqual(receipt['context_bytes'], len(context.encode()))
        for forbidden in ('answer marker', 'question marker', 'synthetic-reader', '/synthetic', self.peer):
            self.assertNotIn(forbidden, json.dumps(receipt))
        # Prepared output may be replayed until stdout has actually flushed.
        self.assertTrue(self.run_hook()); self.assertEqual(len(self.sent), 2)
        receipt['output_written'] = True
        recall_receipts.save_recall_observation(self.value, receipt)
        self.assertEqual(self.run_hook(), {}); self.assertEqual(len(self.sent), 2)

    def test_optional_cache_and_receipt_failures_preserve_hook_output_and_stamp(self):
        observed = []
        write = recall_receipts.metadata.write

        def fail_optional(path, value, **kwargs):
            if path.name.startswith('prompt-'):
                return write(path, value, **kwargs)
            raise OSError('Fictional optional write failure')

        with patch.object(recall_receipts.metadata, 'write', side_effect=fail_optional):
            result = self.run_hook(observe=observed.append)
            self.assertIn('Synthetic L1 answer marker', result['hookSpecificOutput']['additionalContext'])
            self.assertEqual(observed[0]['status'], 'context-prepared')
            self.assertFalse(list(self.root.glob('prompt-*.json')))
            result = io.StringIO()
            with (patch.object(gm, 'load', return_value=self.value),
                  patch.object(gm, 'eligible', return_value=self.local),
                  patch.object(gm, 'submit_hook', return_value={'status': 'staged'}),
                  patch.object(sys, 'stdin', self.prompt()),
                  patch.object(sys, 'stdout', result),
                  patch.object(sys, 'stderr', io.StringIO())):
                self.assertEqual(collection_cli.main(['global-hook']), 0)
            self.assertIn('additionalContext', result.getvalue())
            self.assertEqual(self.run_hook(), {})
            self.assertEqual(len(self.sent), 2)

    def test_stamp_write_failure_still_records_output_and_allows_retry(self):
        write = recall_receipts.metadata.write

        def fail_stamp(path, value, **kwargs):
            if path.name.startswith('prompt-'):
                raise OSError('Fictional prompt stamp failure')
            return write(path, value, **kwargs)

        output, diagnostic = io.StringIO(), io.StringIO()
        with (patch.object(recall_receipts.metadata, 'write', side_effect=fail_stamp),
              patch.object(gm, 'load', return_value=self.value),
              patch.object(gm, 'eligible', return_value=self.local),
              patch.object(gm, 'submit_hook', return_value={'status': 'staged'}),
              patch.object(sys, 'stdin', self.prompt()),
              patch.object(sys, 'stdout', output),
              patch.object(sys, 'stderr', diagnostic)):
            self.assertEqual(collection_cli.main(['global-hook']), 0)
        self.assertIn('Synthetic L1 answer marker', output.getvalue())
        entries = [json.loads(line) for line in diagnostic.getvalue().splitlines()]
        acknowledgement = next(row for row in entries if row.get('status') == 'hook-output-written')
        self.assertIs(acknowledgement['output_written'], True)
        self.assertNotIn('Fictional prompt stamp failure', diagnostic.getvalue())
        self.assertFalse(list(self.root.glob('prompt-*.json')))
        receipt = recall_receipts.recall_observation(self.value, acknowledgement['request_key'])
        self.assertEqual(receipt, recall_receipts.recall_observation(self.value))
        self.assertIs(receipt['output_written'], True)
        self.assertTrue(self.run_hook())
        self.assertEqual(len(self.sent), 2)

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
        groups = self.response['result']['namespace_results']
        groups.extend(copy.deepcopy(group) for _ in range(3))
        expected = set()
        for i, group in enumerate(groups):
            for layer in ('L1', 'L2', 'L3'):
                row = group['recall'][layer] if layer == 'L3' else group['recall'][layer][0]
                row['content'] = f'{i} {layer} {content}'
                expected.add(row['content'])
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
        self.assertEqual({json.loads(line)['content'] for line in context.split('\n')[1:]}, expected)
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
                         {0: first, 2: second})
        self.assertEqual(len(render_context([first, second]).encode('utf-8')), 24576)
        second['content'] += 'x'
        self.assertEqual(select_context_items([first, second], [], '', cached=True), {0: first})

    def test_short_current_source_evidence_keeps_space_when_native_passages_cannot_fit(self):
        group = self.response['result']['namespace_results'][0]
        first = dict(self.row('L1'), id='synthetic-first', content='Background. ' + 'x' * 11000)
        second = dict(self.row('L1'), id='synthetic-second', content='Quartz history. ' + 'y' * 11000)
        target = dict(self.row('L2'), content='Quartz beacon code is UMBER.',
                      source_context=source_context([self.local], self.local))
        group['recall'].update(L1=[first, second], L2=[target], L3={'content': None, 'provenance': {}})

        rows, receipt = self.fitted_hook('What is the Quartz beacon code?')
        self.assertEqual(rows[0]['content'], target['content'])
        self.assertEqual(rows[0]['source_context'], target['source_context'])
        self.assertEqual([row['presentation'] for row in rows[1:]], ['index', 'index'])
        self.assertEqual([row['content'] for row in rows[1:]], ['', ''])
        self.assertEqual(receipt['layer_counts'], {'L1': 2, 'L2': 1, 'L3': 0})
        self.assertEqual(receipt['retrieval']['selected'][0]['record_sha256'], digest(target['id']))
        self.assertEqual([ref['record_sha256'] for ref in receipt['retrieval']['selected'][1:]],
                         [digest(first['id']), digest(second['id'])])
        self.assertEqual(self.sent[-1][1]['limit'], 5)
        self.assertEqual(len(self.sent), 1)

    def test_selects_and_presents_evidence_below_budget(self):
        group = self.response['result']['namespace_results'][0]
        semantic = dict(self.row('L1'), id='synthetic-semantic',
                        content='虚构背景。\n' * 100 + 'At night the crew checks the secondary lantern.')
        target = dict(self.row('L2'), id='synthetic-answer', content='The Kestrel beacon is violet.',
                      source_context=source_context([self.local], self.local))
        key_long = dict(self.row('L2'), id='synthetic-long',
                        content='虚构背景。\n' * 100 + target['content'])
        noise = dict(self.row('L2'), id='synthetic-noise', content='Fictional garden instructions.')
        group['recall'].update(L1=[semantic], L2=[noise, target, key_long],
                               L3={'content': None, 'provenance': {}})

        rows, receipt = self.fitted_hook('What color is the Kestrel beacon?')
        refs = receipt['retrieval']['selected']
        self.assertEqual([ref['record_sha256'] for ref in refs],
                         [digest(row['id']) for row in (target, semantic, key_long, noise)])
        self.assertEqual(rows[0]['content'], target['content'])
        self.assertNotIn('presentation', rows[0])
        self.assertEqual(rows[1]['presentation'], 'excerpt')
        self.assertIn('At night the crew checks the secondary lantern.', rows[1]['content'])
        self.assertIn(rows[1]['content'], semantic['content'])
        self.assertEqual(rows[1]['source_context'], semantic['source_context'])
        self.assertEqual(rows[2]['presentation'], 'excerpt')
        self.assertIn(target['content'], rows[2]['content'])
        self.assertEqual(rows[3]['relevance'], 'assess-against-current-question')
        self.assertEqual(receipt['retrieval']['omitted_for_budget'], 0)

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
        below = select_context_items(items, refs, 'What color is the Kestrel beacon?')
        items[2]['content'] += 'x'
        above = select_context_items(items, refs, 'What color is the Kestrel beacon?')
        self.assertEqual(list(below), [1, 0, 2])
        self.assertEqual(below[2]['presentation'], 'index')
        self.assertEqual(above, below)

    def test_verified_derived_paraphrase_stays_eligible_without_shared_words(self):
        question = '有啥办法把版本退回去？'
        for layer in ('L2', 'L3'):
            with self.subTest(layer=layer):
                group = self.response['result']['namespace_results'][0]
                target = dict(self.row(layer), content='发布出错时使用上一个安装包恢复客户端。')
                group['recall'].update(
                    L1=[], L2=[target] if layer == 'L2' else [],
                    L3=target if layer == 'L3' else {'content': None, 'provenance': {}},
                )
                rows, receipt = self.fitted_hook(question)
                self.assertEqual(receipt['layer_counts'][layer], 1)
                self.assertEqual(receipt['retrieval']['omitted_for_budget'], 0)
                self.assertEqual(rows[0]['content'], target['content'])
                self.assertEqual(rows[0]['source_context'], target['source_context'])
                self.assertEqual(rows[0]['relevance'], 'assess-against-current-question')

    def test_excerpt_keeps_the_counterexample_warning_and_marks_omissions(self):
        group = self.response['result']['namespace_results'][0]
        warning = '不要做下一行，这是曾经误删用户数据的反例。'
        example = '清除旧快照是标准操作。'
        original = '背景说明。\n' * 100 + warning + '\n\n' + example + '\n背景说明。\n' * 100
        target = dict(self.row('L1'), content=original)
        group['recall'].update(L1=[target], L2=[], L3={'content': None, 'provenance': {}})
        rows, receipt = self.fitted_hook('清除旧快照')
        row = rows[0]
        self.assertEqual(row['presentation'], 'excerpt')
        self.assertIn(warning + '\n\n' + example, row['content'])
        self.assertIn(row['content'], original)
        self.assertEqual(row['excerpt_omissions'], {'before': True, 'after': True})
        self.assertEqual(receipt['retrieval']['selected'][0]['output_content_sha256'],
                         digest(row['content']))

    def test_excerpt_prefers_the_scoped_passage_across_multiple_meanings(self):
        group = self.response['result']['namespace_results'][0]
        original = (
            '训练系统可以清除旧快照。\n' + '背景说明。\n' * 100
            + '客户机仍需保留上一份记录。\n\n禁止清除旧快照。\n'
            + '背景说明。\n' * 100
        )
        group['recall'].update(L1=[dict(self.row('L1'), content=original)],
                               L2=[], L3={'content': None, 'provenance': {}})
        rows, _ = self.fitted_hook('客户机可以清除旧快照吗？')
        self.assertEqual(rows[0]['presentation'], 'excerpt')
        self.assertIn('客户机仍需保留上一份记录。\n\n禁止清除旧快照。', rows[0]['content'])
        self.assertNotIn('训练系统可以', rows[0]['content'])

    def test_excerpt_with_unfittable_qualification_becomes_a_body_free_index(self):
        group = self.response['result']['namespace_results'][0]
        original = '不要做下一行，' + '限定条件' * 250 + '。\n清除旧快照是标准操作。'
        group['recall'].update(L1=[dict(self.row('L1'), content=original)],
                               L2=[], L3={'content': None, 'provenance': {}})
        rows, receipt = self.fitted_hook('清除旧快照')
        self.assertEqual(rows[0]['presentation'], 'index')
        self.assertEqual(rows[0]['content'], '')
        self.assertEqual(rows[0]['index_metadata']['description_kind'], 'notice')
        self.assertEqual(rows[0]['lookup_tool'], 'memory_query')
        self.assertEqual(receipt['retrieval']['selected'][0]['content_sha256'], digest(original))

    def test_small_index_has_authorized_name_verbatim_preview_and_offline_receipt(self):
        from hashlib import sha256

        group = self.response['result']['namespace_results'][0]
        preview = '“霜桥”包含 "原文" 和反斜杠 \\。\n请结合完整场景。'
        source_text = preview + '\n' + '背景说明。\n' * 100
        evidence = [{'id': sha256(source_text.encode()).hexdigest(), 'content': source_text}]
        original = '# Evidence Memory\n\n```json\n' + json.dumps(
            evidence, ensure_ascii=False, indent=2
        ) + '\n```'
        path = 'synthetic-internal-directory/恢复演练.md'
        target = dict(self.row('L2'), content=original, path=path)
        target.pop('id')
        group['recall'].update(L1=[], L2=[target], L3={'content': None, 'provenance': {}})
        question = '有啥办法切回上版？'
        self.assertLess(len(original.encode('utf-8')), 24576)

        rows, receipt = self.fitted_hook(question)
        row, ref = rows[0], receipt['retrieval']['selected'][0]
        self.assertEqual(row['presentation'], 'index')
        self.assertEqual(row['content'], '')
        self.assertEqual(row['source_context'], target['source_context'])
        self.assertEqual(row['index_metadata'], {
            'title': '恢复演练', 'title_kind': 'scenario-name',
            'description': preview, 'description_kind': 'source-preview',
        })
        self.assertEqual(ref['record_sha256'], digest(path))
        self.assertEqual(ref['content_sha256'], digest(original))
        self.assertEqual(ref['output_content_sha256'], digest(''))
        self.assertEqual(ref['index_metadata_sha256'], digest(row['index_metadata']))
        self.assertNotIn('synthetic-internal-directory', json.dumps(rows, ensure_ascii=False))
        self.assertNotIn('霜桥', json.dumps(receipt, ensure_ascii=False))

        cache = self.root / ('recall-' + digest(self.local) + '.json')
        saved = json.loads(cache.read_bytes())
        self.assertEqual(saved['items'], rows)
        self.assertEqual(saved['selected'], [ref])
        self.assertNotIn('背景说明', cache.read_text(encoding='utf-8'))
        self.assertNotIn('synthetic-internal-directory', cache.read_text(encoding='utf-8'))

        self.response = {'error': {'code': 'transport_unconfirmed', 'retryable': True}}
        offline, receipt = self.fitted_hook(question)
        self.assertEqual(offline, [dict(row, offline_cache=True, remote_confirmation=False)])
        self.assertEqual(receipt['retrieval']['selected'], [ref])
        self.assertEqual([method for method, _ in self.sent], ['memory_query', 'memory_query'])
        saved['items'][0]['index_metadata']['description'] += '变'
        recall_receipts.metadata.write(cache, saved)
        self.assertEqual(self.run_hook(question=question), {})
        self.assertEqual(recall_receipts.recall_observation(self.value)['status'], 'failed')

    def test_index_without_safe_metadata_has_a_label_and_privacy_preserving_notice(self):
        group = self.response['result']['namespace_results'][0]
        # The sharing marker is a decoded standalone line inside the evidence
        # JSON. A prefix alone would miss it and expose the preceding text.
        source_text = '虚构保密正文。\n[local-only]\n' + '背景说明。\n' * 100
        original = '# Evidence Memory\n\n```json\n' + json.dumps(
            [{'id': 'synthetic-id', 'content': source_text}], ensure_ascii=False
        ) + '\n```'
        target = dict(self.row('L2'), content=original, path='虚构保密场景名.md')
        group['recall'].update(L1=[], L2=[target], L3={'content': None, 'provenance': {}})
        question = '有啥办法切回上版？'
        rows, _ = self.fitted_hook(question)
        self.assertEqual(rows[0]['content'], '')
        self.assertEqual(rows[0]['index_metadata'], {
            'title': 'Scenario memory', 'title_kind': 'record-label',
            'description': 'No safe short source preview is available.', 'description_kind': 'notice',
        })
        self.assertNotIn('保密正文', json.dumps(rows, ensure_ascii=False))
        self.assertNotIn(target['path'], json.dumps(rows, ensure_ascii=False))
        self.assertNotIn('虚构保密场景名', json.dumps(rows, ensure_ascii=False))

        target['content'] = '界' * 500
        target['path'] = 'evidence-' + 'a' * 24 + '.md'
        rows, _ = self.fitted_hook(question)
        self.assertEqual(rows[0]['index_metadata']['title_kind'], 'record-label')
        self.assertEqual(rows[0]['index_metadata']['title'], 'Scenario memory')
        target.pop('path')
        rows, _ = self.fitted_hook(question)
        self.assertEqual(rows[0]['index_metadata']['description_kind'], 'notice')
        target['content'] = '[local-only]\n' + '虚构保密正文。' * 100
        target['path'] = '虚构保密场景名.md'
        self.assertEqual(self.run_hook(question=question), {})
        target['content'] = original
        target['provenance']['sources'][0]['identity']['device_id'] = self.device
        self.assertEqual(self.run_hook(question=question), {})
        self.assertEqual(recall_receipts.recall_observation(self.value)['status'], 'failed')

    def test_index_preview_gives_way_to_notice_at_utf8_budget(self):
        from agent_brain_client.application.memory.context import render_context, select_context_items

        item = {'layer': 'L2', 'content': '界' * 35 + '。\n' + '霜' * 25 + '。\n' + '雪' * 450}
        ref = {'record_sha256': digest('synthetic-index')}
        index = select_context_items([item], [ref], 'unmatched')[0]
        self.assertEqual(index['index_metadata']['description_kind'], 'source-preview')
        index['index_metadata'].update(
            description_kind='notice', description='Source preview omitted to fit the context budget.'
        )
        filler = {'layer': 'L1', 'relevance': 'native-query-match', 'content': 'x' * 40}
        budget = len(render_context([filler, index]).encode('utf-8'))
        with patch('agent_brain_client.application.memory.context.CONTEXT_BYTES', budget):
            selected = select_context_items(
                [filler, item], [{'record_sha256': digest('synthetic-filler')}, ref], 'unmatched'
            )
        self.assertEqual(selected, {0: filler, 1: index})
        self.assertEqual(len(render_context(list(selected.values())).encode('utf-8')), budget)

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
        self.assertEqual(row['excerpt_omissions'], {'before': True, 'after': False})
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

        self.response = {'error': {'code': 'transport_unconfirmed', 'retryable': True}}
        offline, receipt = self.fitted_hook(question)
        self.assertEqual(offline, [dict(row, offline_cache=True, remote_confirmation=False)])
        self.assertEqual(receipt['retrieval']['selected'], [ref])
        self.assertTrue(receipt['offline_cache'])
        self.assertEqual([method for method, _ in self.sent], ['memory_query', 'memory_query'])
        saved['items'][0]['content'] += '变'
        recall_receipts.metadata.write(cache, saved)
        self.assertEqual(self.run_hook(question=question), {})
        self.assertEqual(recall_receipts.recall_observation(self.value)['status'], 'failed')

    def test_budget_prioritizes_corroborated_text_over_unranked_indexes(self):
        group = self.response['result']['namespace_results'][0]
        noise = [
            dict(self.row('L2'), id=f'synthetic-noise-{i}', content=f'Kestrel gardening {i}. ' + '花' * 800)
            for i in range(21)
        ]
        evidence = [
            dict(self.row('L2'), id=f'synthetic-beacon-{i}',
                 content='背景说明。' * 100 + '界' * 105 + '。'
                 + f' Kestrel beacon {i} ' + '界' * 105 + '。' + '界' * 105 + '。')
            for i in range(20)
        ]
        group['recall'].update(L1=[], L2=noise + evidence, L3={'content': None, 'provenance': {}})
        rows, receipt = self.fitted_hook('What about the Kestrel beacon?')
        evidence_by_ref = {digest(row['id']): row for row in evidence}
        retrieval = receipt['retrieval']
        self.assertEqual(retrieval['candidate_count'], 41)
        self.assertEqual(retrieval['omitted_for_budget'], 41 - len(rows))
        self.assertEqual(len(retrieval['selected']), len(rows))
        self.assertIn('index', {row.get('presentation') for row in rows})
        for row, ref in zip(rows, retrieval['selected']):
            if row.get('presentation') == 'index':
                self.assertEqual(row['content'], '')
            else:
                original = evidence_by_ref[ref['record_sha256']]
                self.assertIn(row['content'], original['content'])
                self.assertEqual(row['source_context'], original['source_context'])
            self.assertEqual(ref.get('output_content_sha256', ref['content_sha256']), digest(row['content']))

    def test_both_hooks_keep_short_and_long_semantic_evidence_under_summary_pressure(self):
        from agent_brain_client.adapters.harnesses.codebuddy import events, transcript
        from agent_brain_client.application.memory.context import CONTEXT_BYTES, render_context

        directory = self.root / 'ordinary'
        directory.mkdir()
        self.local = gm.discover(str(directory))
        scope = dict(self.value['scope'], all_projects=True, all_non_projects=True, include_roots=[])
        pin = digest(scope)
        self.value.update(scope=scope, approved_scope_sha256=pin, server_scope_sha256=pin)
        group = self.group()
        self.response['result']['namespace_results'] = [group]
        source = source_context([self.local], self.local)
        sample = dict(layer='L2', scope='personal', content='Departure traffic jam notes 00. ',
                      source_context=source, relevance='assess-against-current-question')
        padding = (CONTEXT_BYTES - len(render_context([sample] * 24).encode('utf-8'))) // (3 * 24)
        noise = [
            dict(self.row('L2'), id=f'synthetic-traffic-{i}',
                 content=f'Departure traffic jam notes {i:02d}. ' + '花' * padding,
                 source_context=source)
            for i in range(66)
        ]
        answer = 'Set out before sunrise. The route is quiet then.'
        long_body = 'The log tracks fictional road work.\n' * 24 + answer
        question = 'Which departure avoids the traffic jam?'
        results = {}
        for content in (answer, long_body):
            target = dict(self.row('L1'), content=content)
            group['recall'].update(L1=[target], L2=noise, L3={'content': None, 'provenance': {}})
            for harness in ('codex', 'codebuddy'):
                with (self.subTest(harness=harness, long=content == long_body),
                      patch.object(gm, 'load', return_value=self.value),
                      patch.object(events, 'capture_messages', return_value={'status': 'staged'}),
                      patch.object(transcript, 'observe', return_value='synthetic'),
                      patch.object(sys, 'stderr', io.StringIO())):
                    self.sent.clear()
                    rows, receipt = self.fitted_hook(question, harness=harness)
                    retrieval = receipt['retrieval']
                    self.assertEqual(retrieval['candidate_count'], 67)
                    self.assertGreater(retrieval['omitted_for_budget'], 0)
                    original = retrieval['returned'][0]
                    self.assertEqual(original['content_sha256'], digest(content))
                    index = next((i for i, ref in enumerate(retrieval['selected'])
                                  if ref['record_sha256'] == original['record_sha256']), None)
                    self.assertIsNotNone(index, receipt['layer_counts'])
                    row = rows[index]
                    self.assertIn(answer, row['content'])
                    self.assertEqual(row['source_context'], target['source_context'])
                    if content == long_body:
                        self.assertEqual(row['presentation'], 'excerpt')
                        self.assertEqual(row['excerpt_omissions'], {'before': True, 'after': False})
                        self.assertIn(row['content'], long_body)
                        self.assertEqual(retrieval['selected'][index]['output_content_sha256'],
                                         digest(row['content']))
                    else:
                        self.assertEqual(row['content'], answer)
                        self.assertNotIn('presentation', row)
                    self.assertEqual([method for method, _ in self.sent], ['memory_query'])
                    if content in results:
                        self.assertEqual(rows, results[content])
                    results[content] = rows

    def test_native_semantic_match_without_keyword_keeps_verified_source_on_overflow(self):
        group = self.response['result']['namespace_results'][0]
        semantic = dict(self.row('L1'), content='Leaving before sunrise avoids rush hour.')
        oversized = dict(self.row('L2'), content='山' * 9000)
        group['recall'].update(L1=[semantic], L2=[oversized], L3={'content': None, 'provenance': {}})
        rows, receipt = self.fitted_hook('Which departure is calmer?')
        self.assertEqual([row['content'] for row in rows], [semantic['content'], ''])
        self.assertEqual(rows[0]['source_context'], semantic['source_context'])
        self.assertEqual(rows[0]['relevance'], 'native-query-match')
        self.assertEqual(rows[1]['presentation'], 'index')
        self.assertEqual(receipt['retrieval']['selected'][0], receipt['retrieval']['returned'][0])

    def test_oversized_native_match_without_a_span_leaves_room_for_unranked_short_source(self):
        group = self.response['result']['namespace_results'][0]
        native = dict(self.row('L1'), content='背景' * 5000 + '失败时取回前一个安装包。')
        scene = dict(self.row('L2'), content='遇到发布故障时，用上个安装包恢复客户端。')
        group['recall'].update(L1=[native], L2=[scene], L3={'content': None, 'provenance': {}})
        rows, receipt = self.fitted_hook('有啥办法把版本退回去？')
        self.assertEqual([row['content'] for row in rows], ['', scene['content']])
        self.assertEqual(rows[0]['presentation'], 'index')
        self.assertEqual(rows[0]['relevance'], 'native-query-match')
        self.assertEqual(rows[1]['relevance'], 'assess-against-current-question')
        self.assertEqual(receipt['retrieval']['selected'][0]['content_sha256'],
                         digest(native['content']))

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
        self.response = {'error': {'code': 'transport_unconfirmed', 'retryable': True}}
        rows, receipt = self.fitted_hook(question)
        self.assertEqual(rows, [
            dict(manual[i], offline_cache=True, remote_confirmation=False) for i in (1, 0)
        ])
        self.assertEqual(receipt['retrieval']['selected'][0]['record_sha256'], digest(target['id']))
        with patch.object(gm, 'approved', return_value=False):
            self.assertEqual(self.run_hook(question=question), {})

    def test_offline_cache_never_reuses_a_different_presentation_mode(self):
        group = self.response['result']['namespace_results'][0]
        question = 'Kestrel beacon'
        original = '背景说明。\n' * 100 + 'The Kestrel beacon is violet.'
        target = dict(self.row('L1'), content=original)
        group['recall'].update(L1=[target], L2=[], L3={'content': None, 'provenance': {}})
        online = copy.deepcopy(self.response)
        offline = {'error': {'code': 'transport_unconfirmed', 'retryable': True}}

        projected, _ = self.fitted_hook(question)
        self.assertEqual(projected[0]['presentation'], 'excerpt')
        self.response = offline
        with self.assertRaisesRegex(ValueError, 'Global recall unavailable'):
            gm.recall(self.value, self.local, 'synthetic-reader', question)
        rows, _ = self.fitted_hook(question)
        self.assertEqual(rows, [dict(projected[0], offline_cache=True, remote_confirmation=False)])

        self.response = online
        whole = gm.recall(self.value, self.local, 'synthetic-reader', question)
        self.assertEqual(whole[0]['content'], original)
        self.assertEqual(whole[0]['source_context'], target['source_context'])
        self.response = offline
        self.assertEqual(self.run_hook(question=question), {})
        receipt = recall_receipts.recall_observation(self.value)
        self.assertEqual(receipt['status'], 'failed')
        self.assertFalse(receipt['offline_cache'])
        self.assertFalse(receipt['output_written'])
        offline_whole = gm.recall(self.value, self.local, 'synthetic-reader', question)
        self.assertEqual(offline_whole, [dict(whole[0], offline_cache=True, remote_confirmation=False)])
        with self.assertRaisesRegex(ValueError, 'Global recall unavailable'):
            gm.recall(self.value, self.local, 'synthetic-reader', 'different question')
        self.assertEqual(len(list(self.root.glob('recall-*.json'))), 1)

    def test_untagged_cache_cannot_replay_projected_content(self):
        group = self.response['result']['namespace_results'][0]
        question = 'Kestrel beacon'
        group['recall'].update(
            L1=[dict(self.row('L1'), content='背景说明。\n' * 100 + 'The Kestrel beacon is violet.')],
            L2=[], L3={'content': None, 'provenance': {}},
        )
        rows, _ = self.fitted_hook(question)
        self.assertEqual(rows[0]['presentation'], 'excerpt')
        cache = self.root / ('recall-' + digest(self.local) + '.json')
        saved = json.loads(cache.read_bytes())
        saved.pop('evidence_selected')
        recall_receipts.metadata.write(cache, saved)
        self.response = {'error': {'code': 'transport_unconfirmed', 'retryable': True}}
        with self.assertRaisesRegex(ValueError, 'Cached recall excerpt changed'):
            gm.recall(self.value, self.local, 'synthetic-reader', question)
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
