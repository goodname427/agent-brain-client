"""Synthetic persistent-memory inventory; never reads native session files."""
import json
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'adapters/codex'))
from content_cache import ContentAdapter,inventory_codex_memories,CacheBlocked,contract,locked
sys.path.insert(0,str(ROOT/'tests'))
import test_content_outbox as base
from content_mcp import ContentParameterError, ContentServer, serve


class CodexMemoryInventory(unittest.TestCase):
    def test_fingerprints_only_memory_tree_and_produces_stable_change_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);memories=home/'memories';memories.mkdir()
            (memories/'summary.md').write_text('synthetic personal memory')
            (memories/'group').mkdir();(memories/'group'/'entry').write_text('synthetic correction')
            prohibited=home/'sessions';prohibited.mkdir();(prohibited/'session.jsonl').write_text('must never scan')
            first=inventory_codex_memories(home);again=inventory_codex_memories(home)
            self.assertEqual(first['status'],'complete')
            self.assertEqual(first['counts'],{'not-checked':2})
            self.assertEqual(first['cursor'],again['cursor'])
            self.assertTrue(all('summary.md' not in json.dumps(row) and 'entry' not in json.dumps(row)
                                for row in first['sources']))
            self.assertTrue(all(row['status']=='not-checked' for row in first['sources']))
            (memories/'group'/'entry').write_text('synthetic corrected fact')
            changed=inventory_codex_memories(home)
            self.assertNotEqual(first['cursor'],changed['cursor'])
            self.assertEqual(len(changed['sources']),2)

    def test_symlinks_are_excluded_and_oversized_scan_never_claims_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);memories=home/'memories';memories.mkdir()
            target=home/'outside';target.write_text('synthetic')
            (memories/'link').symlink_to(target)
            result=inventory_codex_memories(home)
            self.assertEqual(result['counts'],{'excluded':1})
            self.assertEqual(result['sources'][0]['status'],'excluded')
            (memories/'one').write_text('one');(memories/'two').write_text('two')
            limited=inventory_codex_memories(home,max_files=1)
            self.assertEqual(limited['status'],'incomplete')

    def test_absent_memory_root_is_unavailable_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=inventory_codex_memories(Path(tmp))
            self.assertEqual(result['status'],'unavailable')
            self.assertEqual(result['sources'],[])

    def test_private_cache_tracks_inventory_and_import_state_across_restart(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memory_root=helper.home/'.codex/memories';memory_root.mkdir(parents=True)
        source_file=memory_root/'synthetic.md';source_file.write_text('synthetic durable fact')
        inventory=helper.adapter.inventory_memory_sources()
        row=inventory['sources'][0]
        self.assertEqual(inventory['counts'],{'not-checked':1})
        self.assertFalse(inventory['account_complete'])
        record={'title':'Synthetic fact','description':'Synthetic fixture','body':'Synthetic durable fact','type':'feedback'}
        exact=helper.adapter.read_memory_source(row['source_id'],row['source_fingerprint'])
        self.assertEqual(exact['text'],'synthetic durable fact')
        self.assertFalse(exact['data_transferred'])
        helper.adapter.review_memory_source(row['source_id'],row['source_fingerprint'],'shareable','safe-cross-project-personal-summary')
        staged=helper.adapter.stage_memory_import(row['source_id'],row['source_fingerprint'],'memory','synthetic-topic',record)
        self.assertEqual(helper.adapter.import_status()['source_inventory']['counts'],{'inventoried':1})
        helper.adapter.refresh()
        self.assertEqual(helper.cloud.puts,0)
        self.assertEqual(helper.adapter.import_status()['source_inventory']['counts'],{'inventoried':1})
        restarted=helper.restart();restarted.inventory_memory_sources()
        self.assertEqual(restarted.import_status()['source_inventory']['counts'],{'inventoried':1})
        source_file.write_text('synthetic corrected durable fact')
        updated=restarted.inventory_memory_sources()
        self.assertNotEqual(inventory['cursor'],updated['cursor'])
        self.assertEqual(restarted.import_status()['source_inventory']['counts'],{'not-checked':1})
        source_file.unlink();removed=restarted.inventory_memory_sources()
        self.assertEqual(removed['counts'],{})
        self.assertEqual(restarted.import_status()['source_inventory']['counts'],{})

    def test_body_read_requires_exact_inventory_and_reviewed_stage_reuses_import_path(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memory_root=helper.home/'.codex/memories';memory_root.mkdir(parents=True)
        source_file=memory_root/'experience.md';source_file.write_text('Synthetic user-owned project progress, generalized')
        row=helper.adapter.inventory_memory_sources()['sources'][0]
        fake_record={'title':'Synthetic progress','description':'Fixture','body':'Synthetic body','type':'feedback'}
        false_plan={'schema':1,'binding':helper.adapter.binding,
                    'sources':[{'source_id':row['source_id'],'source_fingerprint':row['source_fingerprint'],'kind':'memory','id':'synthetic-topic','record':fake_record}],
                    'coverage':[{'source_id':row['source_id'],'status':'read','reason':'unsubstantiated'}]}
        with self.assertRaises(CacheBlocked):helper.adapter.stage_import(false_plan)
        for status in ('excluded','unavailable','deferred','skipped','duplicate'):
            forged=dict(false_plan,coverage=[{'source_id':row['source_id'],'source_fingerprint':row['source_fingerprint'],
                                             'status':status,'reason':'caller assertion only'}])
            with self.assertRaises(CacheBlocked):helper.adapter.stage_import(forged)
        with self.assertRaises(CacheBlocked):helper.adapter.stage_memory_import(row['source_id'],row['source_fingerprint'],'memory','synthetic-topic',{'title':'x','description':'x','body':'x','type':'feedback'})
        body=helper.adapter.read_memory_source(row['source_id'],row['source_fingerprint'])
        self.assertEqual(body['text'],'Synthetic user-owned project progress, generalized')
        record={'title':'Synthetic progress','description':'Generalized synthetic experience','body':'User made synthetic progress on a personal project.','type':'feedback'}
        with self.assertRaises(CacheBlocked):helper.adapter.stage_memory_import(row['source_id'],row['source_fingerprint'],'memory','synthetic-topic',record)
        helper.adapter.review_memory_source(row['source_id'],row['source_fingerprint'],'shareable','safe-cross-project-personal-summary')
        staged=helper.adapter.stage_memory_import(row['source_id'],row['source_fingerprint'],'memory','synthetic-topic',record)
        status=helper.adapter.import_status()
        self.assertEqual(status['coverage'][0]['source_id'],row['source_id'])
        self.assertEqual(status['coverage'][0]['status'],'read')
        self.assertEqual(status['coverage'][0]['source_fingerprint'],row['source_fingerprint'])
        self.assertNotIn(b'Synthetic user-owned project progress',(helper.cache/'memory-reviews.json').read_bytes())
        self.assertEqual(helper.cloud.calls,[])
        helper.adapter.refresh()
        self.assertEqual(helper.cloud.puts,0)
        saved=helper.adapter.invoke({'request_id':'synthetic_explicit_save_body_001','method':'content_put','params':{
            'kind':'memory','id':'synthetic-topic','record':record,'expected_revision':'missing'}},
            reconciles={'request_ids':[],'source_keys':[staged['source_key']]})
        self.assertNotIn('error',saved)
        self.assertEqual(helper.cloud.records[('memory','synthetic-topic')],record)
        result=helper.adapter.refresh()
        self.assertIn(result['status'],('content-refreshed','content-local-changes-retained','content-current'))
        source_file.write_text('Changed source body')
        changed=helper.adapter.inventory_memory_sources()['sources']
        self.assertIn('not-checked',{item['status'] for item in changed})
        with self.assertRaises(CacheBlocked):helper.adapter.stage_memory_import(row['source_id'],row['source_fingerprint'],'memory','synthetic-topic',record)

    def test_stage_tool_contract_covers_ordinary_progress_and_local_plan_order(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        server=ContentServer(helper.adapter)
        server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'synthetic-test','version':'1'}}})
        server.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        tools=server.handle({'jsonrpc':'2.0','id':1,'method':'tools/list'})['tools']
        by_name={tool['name']:tool for tool in tools}
        stage=by_name['content_sources_stage']['description']
        review=by_name['content_sources_review']['description']
        put_reconcile=by_name['content_put']['inputSchema']['properties']['reconciles']['description']
        self.assertNotIn('Only use for user-provided durable facts',stage)
        for phrase in ('all authorized shareable sources','ordinary user experiences, events, decisions, corrections and short-term progress',
                       'do not filter by importance or long-term durability','content_read for the target',
                       'deduplication, merging and corrections','same source fingerprint is immutable',
                       'local import-plan entry only','returns source_key','content_put','CAS revision',
                       'refresh is not required to perform the write','persistence receipt'):
            self.assertIn(phrase,stage)
        self.assertIn('no-durable-user-fact reason is not a durability or importance threshold',review)
        self.assertIn('exact staged record',put_reconcile)
        self.assertIn('explicit merge',put_reconcile)

    def test_final_experience_candidate_stages_locally_then_saves_with_cas_receipt(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memory_root=helper.home/'.codex/memories';memory_root.mkdir(parents=True)
        source_file=memory_root/'progress.md'
        source_file.write_text('The visitor test exposed a sign confusion; this week I moved the sign and checked the new queue.')
        server=ContentServer(helper.adapter)

        target=server.invoke('content_read',{'kind':'memory','id':'synthetic-topic'})
        self.assertFalse(target['isError'],target)
        self.assertIsNone(target['structuredContent']['result']['record'])
        self.assertEqual(helper.cloud.calls[-1]['method'],'content_read')
        row=helper.adapter.inventory_memory_sources()['sources'][0]
        source=server.invoke('content_sources_read',{'source_id':row['source_id'],'source_fingerprint':row['source_fingerprint']})
        self.assertIn('checked the new queue',source['structuredContent']['text'])
        server.invoke('content_sources_review',{'source_id':row['source_id'],'source_fingerprint':row['source_fingerprint'],
                                                'decision':'shareable','reason_code':'safe-cross-project-personal-summary'})
        final={'title':'Exhibit visitor progress','description':'Recent visitor test improvement',
               'body':'During this week’s visitor test, I corrected a confusing sign and checked that the new placement shortened the queue.',
               'type':'feedback'}
        staged=server.invoke('content_sources_stage',{'source_id':row['source_id'],'source_fingerprint':row['source_fingerprint'],
                                                      'id':'synthetic-topic','record':final})
        self.assertFalse(staged['isError'],staged)
        self.assertFalse(staged['structuredContent']['data_transferred'])
        self.assertFalse(any(call['method']=='content_put' for call in helper.cloud.calls))
        self.assertEqual(helper.adapter.queue_status()['pending'],0)

        altered=dict(final,body=final['body']+' Later change.')
        with self.assertRaises(CacheBlocked):
            helper.adapter.stage_memory_import(row['source_id'],row['source_fingerprint'],'memory','synthetic-topic',altered)

        puts=[call for call in helper.cloud.calls if call['method']=='content_put']
        self.assertEqual(len(puts),0)
        saved=server.invoke('content_put',{'kind':'memory','id':'synthetic-topic','record':final,
              'expected_revision':target['structuredContent']['result']['revision'],
              'request_id':'synthetic_final_experience_save_01',
              'reconciles':{'request_ids':[],'source_keys':[staged['structuredContent']['source_key']]}})
        self.assertFalse(saved['isError'],saved)
        puts=[call for call in helper.cloud.calls if call['method']=='content_put']
        self.assertEqual(len(puts),1)
        self.assertEqual(puts[0]['params']['expected_revision'],'missing')
        self.assertEqual(helper.cloud.records[('memory','synthetic-topic')],final)
        self.assertEqual(helper.adapter.queue_status()['confirmed'],1)
        self.assertEqual(helper.adapter.import_status()['counts'],{'reconciled':1})

    def test_native_stage_remains_local_until_explicit_content_put(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memories=helper.home/'.codex/memories';memories.mkdir(parents=True)
        (memories/'experience.md').write_text('Synthetic experience evidence')
        server=ContentServer(helper.adapter)
        row=helper.adapter.inventory_memory_sources()['sources'][0]
        helper.adapter.read_memory_source(row['source_id'],row['source_fingerprint'])
        helper.adapter.review_memory_source(row['source_id'],row['source_fingerprint'],'shareable','safe-cross-project-personal-summary')
        record={'title':'Synthetic experience','description':'Fixture','body':'Synthetic normalized experience','type':'feedback'}
        staged=helper.adapter.stage_memory_import(row['source_id'],row['source_fingerprint'],'memory','synthetic-topic',record)
        source_key=staged['source_key']
        self.assertEqual(helper.cloud.calls,[])
        helper.adapter.refresh()
        self.assertEqual(helper.cloud.puts,0)
        self.assertEqual(helper.adapter.import_status()['counts'],{'inventoried':1})

        saved=server.invoke('content_put',{'kind':'memory','id':'synthetic-topic','record':record,
              'expected_revision':'missing','request_id':'synthetic_explicit_save_001',
              'reconciles':{'request_ids':[],'source_keys':[source_key]}})
        self.assertFalse(saved['isError'],saved)
        self.assertEqual(helper.cloud.puts,1)
        self.assertEqual(helper.adapter.import_status()['counts'],{'reconciled':1})

    def test_nonshareable_review_revokes_staged_and_queued_import(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memories=helper.home/'.codex/memories';memories.mkdir(parents=True)
        (memories/'experience.md').write_text('Synthetic confidential source fixture')
        row=helper.adapter.inventory_memory_sources()['sources'][0]
        helper.adapter.read_memory_source(row['source_id'],row['source_fingerprint'])
        helper.adapter.review_memory_source(row['source_id'],row['source_fingerprint'],'shareable','safe-cross-project-personal-summary')
        record={'title':'Synthetic experience','description':'Fixture','body':'Synthetic normalized experience','type':'feedback'}
        staged=helper.adapter.stage_memory_import(row['source_id'],row['source_fingerprint'],'memory','synthetic-topic',record)
        source_key=staged['source_key']

        helper.cloud.offline=True
        queued=helper.adapter.invoke({'request_id':'synthetic_revocable_save_001','method':'content_put',
            'params':{'kind':'memory','id':'synthetic-topic','record':record,'expected_revision':'missing'}},
            reconciles={'request_ids':[],'source_keys':[source_key]})
        self.assertEqual(queued['local_queue']['state'],'pending')
        helper.adapter.review_memory_source(row['source_id'],row['source_fingerprint'],'excluded','secret-localonly-or-confidential')
        helper.cloud.offline=False
        helper.adapter.refresh()
        self.assertEqual(helper.cloud.puts,0)
        self.assertEqual(helper.adapter.queue_status()['blocked'],1)
        self.assertEqual(helper.adapter.import_status()['counts'],{'blocked':1})
        with self.assertRaises(CacheBlocked):
            helper.adapter.invoke({'request_id':'synthetic_revocable_save_001','method':'content_put',
                'params':{'kind':'memory','id':'synthetic-topic','record':record,'expected_revision':'missing'}},
                reconciles={'request_ids':[],'source_keys':[source_key]})
        self.assertEqual(helper.cloud.puts,0)

    def test_native_memory_tools_require_memory_policy_scope(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        policy={'schema':1,'adapter':'codex','kinds':['rule'],'ids':{'rule':['synthetic-rule']},
                'dependencies':{'commands':[],'python_modules':[]}}
        helper.adapter=ContentAdapter(helper.home,helper.cache,policy,helper.cloud,codex_home=helper.home/'.codex')
        memories=helper.home/'.codex/memories';memories.mkdir(parents=True)
        (memories/'private.md').write_text('Synthetic fixture; must not be enumerated')
        server=ContentServer(helper.adapter)
        server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'synthetic-test','version':'1'}}})
        server.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        names={tool['name'] for tool in server.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})['tools']}
        self.assertFalse(names & {'content_sources_inventory','content_sources_read','content_sources_review','content_sources_stage'})
        with self.assertRaises(ContentParameterError):
            server.invoke('content_sources_inventory',{})
        self.assertFalse((helper.cache/'memory-inventory.json').exists())

    def test_native_memory_tools_require_memory_grant_even_when_policy_allows_it(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        helper.cloud.grant['artifact_types']=['rule','skill']
        server=ContentServer(helper.adapter)
        server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'synthetic-test','version':'1'}}})
        server.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        names={tool['name'] for tool in server.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})['tools']}
        self.assertFalse(names & {'content_sources_inventory','content_sources_read','content_sources_review','content_sources_stage'})
        with self.assertRaises(ContentParameterError):server.invoke('content_sources_inventory',{})
        self.assertFalse((helper.cache/'memory-inventory.json').exists())

    def test_native_offline_save_conflict_merge_and_durable_reconcile(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memories=helper.home/'.codex/memories';memories.mkdir(parents=True)
        source_file=memories/'experience.md';source_file.write_text('Synthetic local experience version one')
        row=helper.adapter.inventory_memory_sources()['sources'][0]
        helper.adapter.read_memory_source(row['source_id'],row['source_fingerprint'])
        helper.adapter.review_memory_source(row['source_id'],row['source_fingerprint'],'shareable','safe-cross-project-personal-summary')
        original={'title':'Synthetic experience','description':'Original normalized candidate',
                  'body':'A synthetic user experience was normalized locally.','type':'feedback'}
        staged=helper.adapter.stage_memory_import(row['source_id'],row['source_fingerprint'],'memory','synthetic-topic',original)
        source_key=staged['source_key']
        first_request={'request_id':'synthetic_native_offline_save_001','method':'content_put','params':{
            'kind':'memory','id':'synthetic-topic','record':original,'expected_revision':'missing'}}

        helper.cloud.offline=True
        queued=helper.adapter.invoke(first_request,reconciles={'request_ids':[],'source_keys':[source_key]})
        self.assertEqual(queued['local_queue']['state'],'pending')
        source_file.write_text('Synthetic local experience version two')
        helper.adapter.inventory_memory_sources()
        self.assertIn(source_key,helper.adapter._load('memory-reviews.json')['items'])

        remote={'title':'Synthetic concurrent edit','description':'Remote-only detail',
                'body':'A synthetic remote update landed before the offline save replayed.','type':'feedback'}
        helper.cloud.records[('memory','synthetic-topic')]=remote
        helper.cloud.offline=False
        helper.adapter.refresh()
        item=helper.adapter._imports()['items'][source_key]
        self.assertEqual(item['status'],'conflict')
        self.assertEqual(item['request_id'],first_request['request_id'])
        self.assertEqual(item['remote']['record'],remote)

        server=ContentServer(helper.adapter)
        read=server.invoke('content_read',{'kind':'memory','id':'synthetic-topic'})
        self.assertFalse(read['isError'],read)
        self.assertEqual(read['structuredContent']['result']['record'],remote)
        merged={'title':'Synthetic experience and concurrent update','description':'Merged local and remote evidence',
                'body':'The normalized local experience is retained together with the concurrent remote update.',
                'type':'feedback'}
        omitted_source=server.invoke('content_put',{'kind':'memory','id':'synthetic-topic','record':merged,
            'expected_revision':read['structuredContent']['result']['revision'],
            'request_id':'synthetic_native_request_only_001',
            'reconciles':{'request_ids':[first_request['request_id']],'source_keys':[]}})
        self.assertTrue(omitted_source['isError'],omitted_source)
        self.assertEqual(helper.adapter._imports()['items'][source_key]['status'],'conflict')
        self.assertEqual(helper.adapter._load('outbox/'+first_request['request_id']+'.json')['state'],'conflict')
        saved=server.invoke('content_put',{'kind':'memory','id':'synthetic-topic','record':merged,
            'expected_revision':read['structuredContent']['result']['revision'],
            'request_id':'synthetic_native_merged_save_001',
            'reconciles':{'request_ids':[first_request['request_id']],'source_keys':[source_key]}})
        self.assertFalse(saved['isError'],saved)
        self.assertTrue(saved['structuredContent']['persistence']['durable'])
        self.assertEqual(helper.cloud.records[('memory','synthetic-topic')],merged)
        self.assertEqual(helper.adapter._imports()['items'][source_key]['status'],'reconciled')
        self.assertEqual(helper.adapter._load('outbox/'+first_request['request_id']+'.json')['state'],'reconciled')
        self.assertEqual(helper.adapter.queue_status()['confirmed'],1)
        self.assertEqual(helper.adapter.queue_status()['reconciled'],1)

    def test_superseded_fingerprints_are_pruned_from_inventory_and_reviews(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memories=helper.home/'.codex/memories';memories.mkdir(parents=True)
        source=memories/'changing.md';source.write_text('Synthetic version 0')
        for version in range(1030):
            inventory=helper.adapter.inventory_memory_sources()
            row=next(item for item in inventory['sources'] if item['status']=='not-checked')
            helper.adapter.read_memory_source(row['source_id'],row['source_fingerprint'])
            helper.adapter.review_memory_source(row['source_id'],row['source_fingerprint'],'shareable','safe-cross-project-personal-summary')
            source.write_text(f'Synthetic version {version+1}')
        inventory=helper.adapter.inventory_memory_sources()
        self.assertEqual(len(inventory['sources']),1)
        saved=helper.adapter._load('memory-inventory.json')
        self.assertEqual(len(saved['sources']),1)
        reviews=helper.adapter._load('memory-reviews.json')
        self.assertEqual(len(reviews['items']),0)

    def test_stale_native_source_is_reported_as_tool_error_not_json_parse_error(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memories=helper.home/'.codex/memories';memories.mkdir(parents=True)
        source=memories/'stale.md';source.write_text('Synthetic source before inventory')
        row=helper.adapter.inventory_memory_sources()['sources'][0]
        source.write_text('Synthetic changed source after inventory')
        incoming=[
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'synthetic-test','version':'1'}}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'content_sources_read','arguments':{'source_id':row['source_id'],'source_fingerprint':row['source_fingerprint']}}},
        ]
        output=io.StringIO()
        serve(ContentServer(helper.adapter),io.BytesIO(('\n'.join(json.dumps(item) for item in incoming)+'\n').encode()),output)
        result=[json.loads(line) for line in output.getvalue().splitlines()]
        error=next(item for item in result if item.get('id')==2)
        self.assertIn('error',error)
        self.assertNotEqual(error['error']['code'],-32700)

    def test_staged_candidate_direct_cas_put_resolves_existing_and_new_target(self):
        for with_existing in (False,True):
            with self.subTest(with_existing=with_existing):
                helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
                helper.setUp()
                try:
                    original={'title':'Synthetic prior','description':'Prior distinct detail',
                              'body':'The team already keeps a checklist for this event.','type':'feedback'}
                    if with_existing:helper.cloud.records[('memory','synthetic-topic')]=original
                    memory_root=helper.home/'.codex/memories';memory_root.mkdir(parents=True)
                    source_file=memory_root/'experience.md';source_file.write_text('This week I corrected the sign and checked the shorter queue.')
                    server=ContentServer(helper.adapter)
                    target=server.invoke('content_read',{'kind':'memory','id':'synthetic-topic'})['structuredContent']['result']
                    revision=target['revision']
                    row=helper.adapter.inventory_memory_sources()['sources'][0]
                    server.invoke('content_sources_read',{'source_id':row['source_id'],'source_fingerprint':row['source_fingerprint']})
                    server.invoke('content_sources_review',{'source_id':row['source_id'],'source_fingerprint':row['source_fingerprint'],
                                                            'decision':'shareable','reason_code':'safe-cross-project-personal-summary'})
                    record={'title':'Synthetic current experience','description':'Recent change with prior detail preserved',
                            'body':(original['body']+' This week I corrected the sign and checked the shorter queue.') if with_existing else 'This week I corrected the sign and checked the shorter queue.',
                            'type':'feedback'}
                    staged=server.invoke('content_sources_stage',{'source_id':row['source_id'],'source_fingerprint':row['source_fingerprint'],
                                                                  'id':'synthetic-topic','record':record})
                    self.assertFalse(staged['isError'],staged)
                    self.assertFalse(staged['structuredContent']['data_transferred'])
                    source_key=staged['structuredContent']['source_key']
                    self.assertEqual(helper.cloud.calls[-1]['method'],'content_read')
                    changed=dict(record,body=record['body']+' Not in staged candidate.')
                    rejected=server.invoke('content_put',{'kind':'memory','id':'synthetic-topic','record':changed,
                                                          'expected_revision':revision,'request_id':'synthetic_bad_put_0001',
                                                          'reconciles':{'request_ids':[],'source_keys':[source_key]}})
                    self.assertTrue(rejected['isError'],rejected)
                    self.assertFalse(any(call['method']=='content_put' for call in helper.cloud.calls))
                    request_id='synthetic_direct_put_'+('existing' if with_existing else 'new')
                    saved=server.invoke('content_put',{'kind':'memory','id':'synthetic-topic','record':record,
                                                       'expected_revision':revision,'request_id':request_id,
                                                       'reconciles':{'request_ids':[],'source_keys':[source_key]}})
                    self.assertFalse(saved['isError'],saved)
                    self.assertTrue(saved['structuredContent']['persistence']['durable'])
                    put=next(call for call in helper.cloud.calls if call['method']=='content_put')
                    self.assertEqual(put['params']['expected_revision'],revision)
                    self.assertNotIn('reconciles',put['params'])
                    self.assertEqual(helper.cloud.records[('memory','synthetic-topic')],record)
                    self.assertEqual(helper.adapter.import_status()['counts'],{'reconciled':1})
                    self.assertEqual(helper.adapter._imports()['items'][source_key]['request_id'],request_id)
                finally:
                    helper.doCleanups()

    def test_staged_direct_cas_offline_replay_precedes_automatic_import(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp()
        try:
            memory_root=helper.home/'.codex/memories';memory_root.mkdir(parents=True)
            source_file=memory_root/'progress.md';source_file.write_text('Synthetic short-term progress')
            server=ContentServer(helper.adapter)
            target=server.invoke('content_read',{'kind':'memory','id':'synthetic-topic'})
            revision=target['structuredContent']['result']['revision']
            row=helper.adapter.inventory_memory_sources()['sources'][0]
            server.invoke('content_sources_read',{'source_id':row['source_id'],'source_fingerprint':row['source_fingerprint']})
            server.invoke('content_sources_review',{'source_id':row['source_id'],'source_fingerprint':row['source_fingerprint'],
                                                    'decision':'shareable','reason_code':'safe-cross-project-personal-summary'})
            record={'title':'Synthetic recent progress','description':'Synthetic offline retry candidate',
                    'body':'Synthetic short-term progress','type':'feedback'}
            staged=server.invoke('content_sources_stage',{'source_id':row['source_id'],'source_fingerprint':row['source_fingerprint'],
                                                          'id':'synthetic-topic','record':record})
            source_key=staged['structuredContent']['source_key']
            helper.cloud.offline=True
            queued=server.invoke('content_put',{'kind':'memory','id':'synthetic-topic','record':record,
                                                'expected_revision':revision,'request_id':'synthetic_offline_direct_001',
                                                'reconciles':{'request_ids':[],'source_keys':[source_key]}})
            self.assertEqual(queued['structuredContent']['local_queue']['state'],'pending')
            self.assertFalse(queued['structuredContent']['persistence']['durable'])
            self.assertEqual(helper.adapter.import_status()['counts'],{'inventoried':1})
            helper.cloud.offline=False
            helper.adapter.refresh()
            self.assertEqual(helper.adapter.import_status()['counts'],{'reconciled':1})
            self.assertEqual(helper.adapter.queue_status(),{'pending':0,'confirmed':1,'conflict':0,'blocked':0,'reconciled':0})
            requests=[call['request_id'] for call in helper.cloud.calls if call['method']=='content_put']
            self.assertEqual(requests,['synthetic_offline_direct_001','synthetic_offline_direct_001'])
        finally:
            helper.doCleanups()

    def test_inventory_alone_cannot_be_reviewed_or_staged(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memory_root=helper.home/'.codex/memories';memory_root.mkdir(parents=True)
        (memory_root/'experience.md').write_text('Synthetic memory fixture')
        row=helper.adapter.inventory_memory_sources()['sources'][0]
        with self.assertRaises(CacheBlocked):
            helper.adapter.review_memory_source(row['source_id'],row['source_fingerprint'],'shareable','safe-cross-project-personal-summary')
        key=__import__('hashlib').sha256(contract.encode([row['source_id'],row['source_fingerprint']])).hexdigest()
        with locked(helper.adapter.cache/'content.lock'):
            inventory=helper.adapter._load('memory-inventory.json')
            inventory['sources'][key]['status']='inventoried'
            helper.adapter._save('memory-inventory.json',inventory)
        with self.assertRaises(CacheBlocked):
            helper.adapter.stage_memory_import(row['source_id'],row['source_fingerprint'],'memory','synthetic-topic',
                {'title':'Fixture','description':'Fixture','body':'Fixture','type':'feedback'})

    def test_memory_tree_traversal_bounds_directory_fanout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'memories';root.mkdir()
            for i in range(8):(root/f'item-{i}').write_text('x')
            result=inventory_codex_memories(tmp,max_entries=4)
            self.assertEqual(result['status'],'incomplete')
            self.assertIn('bounded directory inventory limit',result['reason'])

    def test_null_fingerprint_coverage_is_allowed_only_for_inventory_exclusions(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memories=helper.home/'.codex/memories';memories.mkdir(parents=True)
        outside=helper.home/'outside';outside.write_text('synthetic')
        (memories/'link').symlink_to(outside)
        row=helper.adapter.inventory_memory_sources()['sources'][0]
        self.assertIsNone(row['source_fingerprint'])
        plan={'schema':1,'binding':helper.adapter.binding,'sources':[],
              'coverage':[{'source_id':row['source_id'],'source_fingerprint':None,'status':'excluded','reason':'inventory excluded symlink'}]}
        helper.adapter.stage_import(plan)
        self.assertEqual(helper.adapter.import_status()['coverage'][0]['status'],'excluded')
        forged=dict(plan,coverage=[{'source_id':row['source_id'],'source_fingerprint':None,'status':'read','reason':'forged'}])
        with self.assertRaises(CacheBlocked):helper.adapter.stage_import(forged)

    def test_local_inventory_tool_pages_statuses_and_invalidates_changed_cursor(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memory_root=helper.home/'.codex/memories';memory_root.mkdir(parents=True)
        for index in range(70):(memory_root/f'synthetic-{index:03}.md').write_text(f'synthetic {index}')
        server=ContentServer(helper.adapter)
        first=server.invoke('content_sources_inventory',{})['structuredContent']
        self.assertEqual(len(first['sources']),64);self.assertIsNotNone(first['next_cursor'])
        self.assertNotIn('synthetic 0',json.dumps(first))
        second=server.invoke('content_sources_inventory',{'cursor':first['next_cursor']})['structuredContent']
        self.assertEqual(len(second['sources']),6);self.assertIsNone(second['next_cursor'])
        self.assertEqual(first['cursor'],second['cursor'])
        (memory_root/'synthetic-000.md').write_text('synthetic changed')
        with self.assertRaises(ContentParameterError):
            server.invoke('content_sources_inventory',{'cursor':first['next_cursor']})
        selected=first['sources'][0]
        body=server.invoke('content_sources_read',{'source_id':selected['source_id'],'source_fingerprint':selected['source_fingerprint']})['structuredContent']
        self.assertIn('synthetic ',body['text'])
        server.invoke('content_sources_review',{'source_id':selected['source_id'],'source_fingerprint':selected['source_fingerprint'],'decision':'shareable','reason_code':'safe-cross-project-personal-summary'})
        staged=server.invoke('content_sources_stage',{'source_id':selected['source_id'],'source_fingerprint':selected['source_fingerprint'],'id':'synthetic-topic','record':{'title':'Synthetic source','description':'Synthetic only','body':'Synthetic normalized fact','type':'feedback'}})
        self.assertFalse(staged['structuredContent']['data_transferred'])
        self.assertEqual(helper.cloud.calls,[])


if __name__=='__main__':unittest.main()
