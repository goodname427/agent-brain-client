"""Synthetic acceptance regressions; no real credentials, sources or network."""
import copy
import json
from types import SimpleNamespace
import unittest
import unicodedata
from unittest.mock import Mock, patch

import test_content_outbox as fixture
from content_mcp import ContentServer, ProtocolError
from https_content import HTTPSContentClient
import global_memory as gm
import global_memory_install as install
from global_memory_scope import digest, namespace, source_context


class WindowsAcceptanceRepairs(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.Outbox('test_same_id_concurrent_retry_only_uploads_once')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.adapter = self.fixture.adapter
        self.device, self.peer = 'a' * 32, 'b' * 32
        self.local = {'kind':'project','cwd':'c:/synthetic/current','project_root':'c:/synthetic/current','repository_root':'c:/synthetic/current'}
        self.foreign = {'kind':'project','cwd':'/synthetic/other','project_root':'/synthetic/other','repository_root':'/synthetic/other'}
        self.project = namespace('synthetic-owner', 'c' * 32)
        self.params = {'origin':self.local,'session_id':'synthetic-reader','query':'synthetic progress','limit':3}
        self.client = HTTPSContentClient.__new__(HTTPSContentClient)
        self.client.device_id = self.device
        self.client.grant = self.fixture.cloud.grant
        self.client.device_task = self.client.recall_policy = None
        self.client.transport = SimpleNamespace(request=self.response)
        self.fixture.cloud.device_id = self.device
        self.fixture.cloud.recall = self.client.recall

    def row(self, layer):
        return {'content':'Synthetic ' + layer, 'source_context':source_context([self.foreign], self.local),
                'provenance':{'status':'verified-native-generation-graph','generation_ids':['generation'],
                              'event_ids':['event'],'model_output_truth_verified':False,
                              'sources':[{'event_id':'event','state':'committed','identity':{'project_id':self.project,'device_id':self.peer}}]}}

    def response(self, request):
        return {'request_id':request['request_id'],'result':{'schema':1,'namespace_results':[
            {'project_id':self.project,'sharing':'personal','source_device_id':self.peer,
             'recall':{'L0':[],'L1':[self.row('L1')],'L2':[self.row('L2')],'L3':self.row('L3'),
                       'cross_session':True,'model_extraction_verified':False}}]},
                'persistence':{'state':'read-only','durable':False}}

    def test_real_mcp_adapter_https_read_keeps_three_layers_and_proof(self):
        server = ContentServer(self.adapter, '0.6.3'); server.state = 'ready'
        listed = server.handle({'jsonrpc':'2.0','id':1,'method':'tools/list'})['tools']
        tool = next(t for t in listed if t['name'] == 'memory_query')
        self.assertTrue(tool['annotations']['readOnlyHint'])
        self.assertNotIn('memory_recall',[t['name'] for t in listed])
        with patch.object(self.adapter, 'refresh', side_effect=AssertionError('No mapping refresh')), patch.object(self.adapter, '_drain', side_effect=AssertionError('No queue replay')):
            answer = server.handle({'jsonrpc':'2.0','id':2,'method':'tools/call',
                                    'params':{'name':'memory_query','arguments':{k:v for k,v in self.params.items() if k != 'limit'}}})
        self.assertFalse(answer['isError'], answer)
        found = answer['structuredContent']['result']['namespace_results'][0]['recall']
        self.assertEqual(found['L2'][0]['content'], 'Synthetic L2')
        self.assertEqual(found['L3']['provenance']['event_ids'], ['event'])
        self.assertFalse(found['L3']['source_context']['global_rule'])
        self.assertFalse((self.fixture.cache / 'outbox').exists())

    def test_mcp_keeps_l2_l3_for_nfc_nfd_equivalent_mac_and_windows_origins(self):
        for root in ('/synthetic/projects/caf\u00e9', 'C:\\Synthetic\\Projects\\caf\u00e9'):
            for reader_form, source_form in (('NFC', 'NFD'), ('NFD', 'NFC')):
                with self.subTest(root=root, reader=reader_form, source=source_form):
                    reader = unicodedata.normalize(reader_form, root)
                    source = unicodedata.normalize(source_form, root)
                    self.local = {'kind':'project','cwd':reader,'project_root':reader,'repository_root':reader}
                    self.foreign = {'kind':'project','cwd':source,'project_root':source,'repository_root':source}
                    self.params['origin'] = self.local
                    before = copy.deepcopy(self.params)
                    server = ContentServer(self.adapter, '0.6.3'); server.state = 'ready'
                    result = server.handle({'jsonrpc':'2.0','id':2,'method':'tools/call',
                                            'params':{'name':'memory_query','arguments':self.params}})
                    self.assertFalse(result['isError'], result)
                    found = result['structuredContent']['result']['namespace_results'][0]['recall']
                    self.assertEqual(found['L2'][0]['content'], 'Synthetic L2')
                    self.assertEqual(found['L3']['content'], 'Synthetic L3')
                    self.assertEqual(found['L3']['source_context']['relation'], 'current-source')
                    self.assertEqual(found['L3']['source_context']['origins'], [self.foreign])
                    self.assertEqual(self.params, before)

    def test_denial_is_preserved_and_never_retries_or_enables_sources(self):
        calls = []
        def deny(request):
            calls.append(copy.deepcopy(request))
            return {'request_id':request['request_id'],'error':{'code':'global_source_device_denied','retryable':False},'persistence':{'durable':False}}
        self.client.transport.request = deny
        result = ContentServer(self.adapter).invoke('memory_query', self.params)['structuredContent']
        self.assertEqual(result['error']['code'], 'global_source_device_denied')
        self.assertEqual(result['source_admission']['status'], 'device-source-approval-required')
        self.assertEqual([r['method'] for r in calls], ['memory_query'])
        self.assertFalse(gm.settings_path(self.fixture.home).exists())

    def test_forged_proof_raw_events_wrong_scope_and_excess_results_fail_closed(self):
        changes = [lambda r:r['L3']['provenance']['sources'][0]['identity'].update(device_id=self.device),
                   lambda r:r['L3']['source_context'].update(global_rule=True),
                   lambda r:r['L3']['source_context'].update(relation='current-source'),
                   lambda r:r.update(L0=[{'provenance':{}}]),
                   lambda r:r['L3']['provenance'].update(event_ids=['unconfirmed'])]
        for change in changes:
            def reply(request):
                result = self.response(request); change(result['result']['namespace_results'][0]['recall']); return result
            self.client.transport.request = reply
            with self.subTest(change=change): self.assertTrue(ContentServer(self.adapter).invoke('memory_query', self.params)['isError'])
        self.client.transport.request = self.response
        self.assertTrue(ContentServer(self.adapter).invoke('memory_query', dict(self.params, limit=1))['isError'])
        with self.assertRaises(ProtocolError): ContentServer(self.adapter).invoke('memory_query', dict(self.params, enable=True))
        self.fixture.cloud.grant['scopes'] = ['put']
        with self.assertRaises(ProtocolError): ContentServer(self.adapter).invoke('memory_query', self.params)

    def test_manual_writer_device_can_differ_from_storage_device_and_unknown_context_is_explicit(self):
        for origin in (self.local,None):
            proof={'status':'verified-l1-source-graph','event_ids':[],'generation_ids':[],
                   'sources':[],'model_output_truth_verified':False,'manual_sources':[{
                    'kind':'agent','actor':'synthetic-owner','device_id':self.device,
                    'identity':{'device_id':self.device,'project_id':self.project,'session_id':None},
                    'origin':origin,'original_id':'synthetic-direct','revision':'d'*64,'record_id':'abm-'+'e'*64,'state':'committed'}]}
            row={'content':'Synthetic direct fact','provenance':proof,
                 'source_context':source_context([] if origin is None else [origin],self.local)}
            def response(request):
                return {'request_id':request['request_id'],'result':{'schema':1,'namespace_results':[
                    {'project_id':self.project,'sharing':'personal','source_device_id':self.peer,
                     'recall':{'L0':[],'L1':[row],'L2':[],'L3':{'content':None,'provenance':{}},
                               'cross_session':True,'model_extraction_verified':False}}]}}
            self.client.transport.request=response
            result=ContentServer(self.adapter).invoke('memory_query',self.params)
            self.assertFalse(result['isError'],result)
            if origin is None:self.assertEqual(row['source_context']['relation'],'unknown-source')
            proof['manual_sources'][0]['identity']['device_id']=self.peer
            self.assertTrue(ContentServer(self.adapter).invoke('memory_query',self.params)['isError'])

    def test_empty_never_approved_install_can_explicitly_select_existing_pool(self):
        home = self.fixture.home
        file = gm.settings_path(home)
        value = {'schema':1,'enabled':False,'device_home':str(home),
                 'scope':{'schema':1,'profile_id':'a'*32,'include_roots':[],'exclude_roots':[],'non_project_roots':[],'recall_personal':True},
                 'approved_scope_sha256':None,'server_scope_sha256':None,'legacy_projects':[],
                 'operator':None,'operator_sha256':None,'code_sha256':{}}
        from brain_lock import private_directory
        private_directory(file.parent)
        gm.old.write(file, value)
        args = SimpleNamespace(device_home=str(home), profile_id='b'*32, operator=None, operator_sha256=None)
        with patch.object(gm.old, 'registration', return_value=(None, {'runtime':{'codex_home':str(home)}})), patch.object(install, 'transact', return_value={}):
            result = install.install(args)
            self.assertFalse(result['expanded_sources_enabled'])
            stored = json.loads(file.read_bytes()); self.assertEqual(stored['scope']['profile_id'], 'b'*32)
            self.assertIsNone(stored['approved_scope_sha256'])
            stored['approved_scope_sha256'] = digest(stored['scope']); gm.old.write(file, stored)
            install.install(SimpleNamespace(**dict(vars(args), profile_id='c'*32)))
            stored = gm.old.private_json(file)
            self.assertEqual(stored['scope']['profile_id'], 'c'*32)
            self.assertEqual(stored['approved_scope_sha256'], digest(stored['scope']))

    def test_local_status_lists_all_fences_without_credentials_or_source_reads(self):
        value = {'device_home':str(self.fixture.home),'enabled':False,
                 'scope':{'schema':1,'profile_id':'a'*32,'include_roots':[],'exclude_roots':[],'non_project_roots':[],'recall_personal':True},
                 'approved_scope_sha256':None,'server_scope_sha256':None}
        with patch.object(gm, 'GlobalClient', side_effect=AssertionError('No credentials')):
            result = gm.admission_state(value)
        self.assertEqual(result['reasons'], ['capture-disabled','source-scope-empty','local-source-approval-required','server-source-approval-unconfirmed'])
        self.assertFalse(result['permissions_changed']); self.assertFalse(result['historical_import'])

    def test_approved_configuration_uses_https_and_keeps_fence_when_unconfirmed(self):
        import global_memory_operator as operator
        from brain_lock import private_directory
        directory = gm.settings_path(self.fixture.home).parent
        private_directory(directory)
        scope = {'schema':1,'profile_id':'c'*32,'include_roots':[{'root':str(self.fixture.home),'sharing':'personal'}],
                 'exclude_roots':[],'non_project_roots':[],'recall_personal':True}
        value = {'device_home':str(self.fixture.home),'scope':scope,'enabled':False,'legacy_projects':[],
                 'approved_scope_sha256':None,'server_scope_sha256':None,'operator':None}
        profile = directory / 'synthetic-profile.json'
        gm.old.write(profile, {'endpoint':'https://synthetic.invalid','grant':{'actor':'synthetic-owner','scopes':['read','put']}})
        runtime = {'https_profile':str(profile),'https_profile_sha256':gm.old.digest(profile.read_bytes()),'content_policy_sha256':digest('policy')}
        args = SimpleNamespace(device_home=str(self.fixture.home), include_root=[], isolate_root=[], exclude_root=[],
                               non_project_root=[], approve_scope=digest(scope), packet_output=None, activate_packet=None)
        client = SimpleNamespace(device_id=self.device, wire=Mock(side_effect=[
            {'result':{'metadata_only':True,'source_registered':False,'source_device_id':self.device,'global_profile_id':scope['profile_id']}},
            {'error':{'code':'transport_unconfirmed','retryable':True}}]))
        with patch.object(gm, 'load', return_value=value), patch.object(gm.old, 'registration', return_value=(None, {'runtime':runtime})), patch.object(gm, 'GlobalClient', return_value=client), patch.object(operator, 'operator', side_effect=AssertionError('No SSH operation')):
            result = operator.configure(args)
        self.assertEqual(result['status'], 'global-memory-registration-pending')
        packet = gm.old.private_json(result['packet'])
        self.assertEqual(packet['schema'], 2); self.assertEqual(packet['scope'], dict(scope, all_projects=True))
        self.assertEqual(packet['client']['source_device_id'], self.device)
        self.assertEqual(client.wire.call_args.args, ('memory_global_register',packet,'source_'+digest(packet)))
        self.assertFalse((directory / 'global.off').exists())
        self.assertFalse(gm.settings_path(self.fixture.home).exists())

    def test_failed_activation_reports_exact_denial_without_enabling(self):
        import global_memory_operator as operator
        value = {'device_home':str(self.fixture.home),'enabled':False}
        scope = {'schema':1,'profile_id':'c'*32,'include_roots':[{'root':str(self.fixture.home),'sharing':'personal'}],
                 'exclude_roots':[],'non_project_roots':[],'recall_personal':True}
        error = {'code':'global_source_device_denied','retryable':False}
        client = SimpleNamespace(device_id=self.device, wire=lambda *args: {'error':error})
        with patch.object(gm, 'GlobalClient', return_value=client): result = operator.activate(value, scope)
        self.assertFalse(result['activated']); self.assertEqual(result['server_error'], error)
        self.assertFalse(value['enabled']); self.assertFalse(gm.settings_path(self.fixture.home).exists())


if __name__ == '__main__': unittest.main()
