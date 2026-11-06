"""Revision-bound inventory pages and complete verified snapshots; synthetic only."""
import copy
import json
import unittest

import test_content_outbox as fixture
from agent_brain_client.interfaces.mcp.content import ContentServer


class CatalogPages(unittest.TestCase):
    def setUp(self):
        self.f=fixture.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.cloud.records={('memory','synthetic-'+str(n).zfill(4)):dict(fixture.MEMORY,body='Synthetic fact '+str(n)) for n in range(600)}
        self.f.cloud.puts=1

    def request(self,cursor=None):
        return {'request_id':'synthetic_catalog_page_0001','method':'content_catalog','params':dict(kinds=['memory'],**({'cursor':cursor} if cursor else {}))}

    def pages(self,adapter):
        cursor=None;items=[];sizes=[]
        while True:
            response=adapter.invoke(self.request(cursor));self.assertNotIn('error',response)
            page=response['result'];items+=page['artifacts'];sizes.append(len(page['artifacts']))
            cursor=page.get('next_cursor')
            if cursor is None:return response,items,sizes

    def test_live_inventory_and_refresh_cover_all_pages_then_reuse_snapshot_after_restart(self):
        _,items,sizes=self.pages(self.f.adapter)
        self.assertEqual(sizes,[256,256,88]);self.assertEqual(len({row['id'] for row in items}),600)
        self.assertEqual(self.f.adapter.refresh()['status'],'content-refreshed')
        active=json.loads((self.f.cache/'active.json').read_bytes())
        snapshot=json.loads((self.f.cache/'snapshots'/(active['commit']+'.json')).read_bytes())
        self.assertEqual(len(snapshot['records']),600)
        self.f.cloud.calls=[]
        restarted=self.f.restart();self.assertIn(restarted.refresh()['status'],('content-refreshed','content-current'))
        self.assertFalse(any(r['method']=='content_read' for r in self.f.cloud.calls))
        self.f.cloud.offline=True
        reply=restarted.invoke({'request_id':'synthetic_after_page_0001','method':'content_read','params':{'kind':'memory','id':'synthetic-0599'}})
        self.assertTrue(reply['local_cache']['stale']);self.assertEqual(reply['result']['record']['body'],'Synthetic fact 599')
        response,offline,sizes=self.pages(restarted)
        self.assertTrue(response['local_cache']['stale']);self.assertEqual(sizes,[256,256,88]);self.assertEqual(offline,items)

    def test_changed_snapshot_rejects_continuation_and_preserves_prior_verified_cache(self):
        self.assertEqual(self.f.adapter.refresh()['status'],'content-refreshed')
        active=(self.f.cache/'active.json').read_bytes()
        first=self.f.adapter.invoke(self.request())['result']
        self.f.cloud.records['memory','synthetic-new']=copy.deepcopy(fixture.MEMORY);self.f.cloud.puts+=1
        rejected=self.f.adapter.invoke(self.request(first['next_cursor']))
        self.assertEqual(rejected['error']['code'],'catalog_changed')
        self.assertEqual((self.f.cache/'active.json').read_bytes(),active)
        self.f.cloud.offline=True
        cached=self.f.adapter.invoke({'request_id':'synthetic_race_read_0001','method':'content_read','params':{'kind':'memory','id':'synthetic-0599'}})
        self.assertTrue(cached['local_cache']['stale'])
        self.assertFalse(json.loads((self.f.cache/'offline-snapshot.json').read_bytes()).get('read_denied'))
        self.f.cloud.offline=False
        self.assertEqual(len(self.pages(self.f.adapter)[1]),601)

    def test_refresh_page_race_keeps_prior_snapshot_available_offline(self):
        self.assertEqual(self.f.adapter.refresh()['status'],'content-refreshed')
        original=self.f.cloud.request
        def racing(request):
            if request['method']=='content_catalog' and request['params'].get('cursor'):
                self.f.cloud.puts+=1
            return original(request)
        self.f.cloud.request=racing
        self.assertNotEqual(self.f.adapter.refresh()['status'],'content-refreshed')
        self.f.cloud.offline=True
        found=self.f.adapter.invoke({'request_id':'synthetic_refresh_race_0001','method':'content_read','params':{'kind':'memory','id':'synthetic-0599'}})
        self.assertTrue(found['local_cache']['stale'])

    def test_scope_filter_can_empty_nonfinal_pages_without_hiding_later_allowed_ids(self):
        original=self.f.cloud.request
        def filtered(request):
            response=original(request)
            if request['method']=='content_catalog' and 'result' in response:
                response['result']['artifacts']=[row for row in response['result']['artifacts'] if row['id']=='synthetic-0599']
            return response
        self.f.cloud.request=filtered
        _,items,sizes=self.pages(self.f.adapter)
        self.assertEqual(sizes,[0,0,1]);self.assertEqual(items[0]['id'],'synthetic-0599')
        self.assertEqual(self.f.adapter.refresh()['status'],'content-refreshed')
        self.f.cloud.offline=True
        found=self.f.adapter.invoke({'request_id':'synthetic_filtered_page_0001','method':'content_read','params':{'kind':'memory','id':'synthetic-0599'}})
        self.assertTrue(found['local_cache']['stale'])

    def test_offline_automation_finds_later_page_matches_and_stops_when_full(self):
        from unittest.mock import patch
        import agent_brain_client.adapters.harnesses.codex.project as project_memory
        self.assertEqual(self.f.adapter.refresh()['status'],'content-refreshed')
        self.f.cloud.offline=True
        settings={'profile':'fictional-profile','profile_sha256':'a'*64,'content_policy':'fictional-content',
            'content_policy_sha256':'b'*64,'recall_policy':None,'device_home':str(self.f.home),
            'cache':str(self.f.cache),'codex_home':str(self.f.home/'.codex'),'max_items':1}
        def private(path,pin):return {'grant':self.f.cloud.grant} if path=='fictional-profile' else fixture.POLICY
        original=self.f.adapter.invoke
        def invoke(request):
            if request['method']=='memory_query':return {'error':{'code':'transport_unconfirmed','retryable':True}}
            return original(request)
        with patch.object(project_memory,'private_json',side_effect=private), \
                patch('agent_brain_client.adapters.server.https.HTTPSContentClient',return_value=self.f.cloud), \
                patch('agent_brain_client.adapters.harnesses.codex.content.CodexContentSession',return_value=self.f.adapter), \
                patch.object(self.f.adapter,'invoke',side_effect=invoke):
            for query,pages in (('0',1),('599',3)):
                with self.subTest(query=query),patch.object(self.f.adapter,'_offline_read',wraps=self.f.adapter._offline_read) as read:
                    result=project_memory.recalled(dict(settings,query=query),'fictional-session')
                    self.assertEqual(len(result),1);self.assertEqual(result[0]['id'],'synthetic-'+query.zfill(4))
                    self.assertTrue(result[0]['cached_snapshot'])
                    self.assertEqual(sum(c.args[0]['method']=='content_catalog' for c in read.call_args_list),pages)

    def test_tool_schema_exposes_optional_cursor_and_keeps_one_memory_query(self):
        server=ContentServer(self.f.adapter);server.state='ready'
        tools=server.handle({'jsonrpc':'2.0','id':1,'method':'tools/list'})['tools']
        catalog=next(row for row in tools if row['name']=='content_catalog')['inputSchema']
        self.assertEqual(catalog['required'],['kinds']);self.assertIn('cursor',catalog['properties'])
        self.assertNotIn('memory_recall',[row['name'] for row in tools])


if __name__=='__main__':unittest.main()
