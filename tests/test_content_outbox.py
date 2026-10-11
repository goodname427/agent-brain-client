"""Offline/restart/import failure tests with synthetic sources and fake homes."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'adapters/codex'));sys.path.insert(0,str(ROOT/'scripts'));sys.path.insert(0,str(ROOT/'tools'))
from content_cache import ContentAdapter,CacheBlocked
import content_contract as contract

MEMORY={'title':'Synthetic','description':'Synthetic only','body':'Synthetic local content','type':'feedback'}
RULE={'title':'Synthetic rule','body':'Synthetic rule body','enabled':False,'trigger':{'mode':'on-demand','patterns':[]}}
SKILL={'description':'Synthetic skill','enabled':False,'files':{'SKILL.md':'---\nname: synthetic-skill\ndescription: Synthetic skill\n---\nSynthetic only'},'dependencies':{'commands':[],'python_modules':[]}}
POLICY={'schema':1,'adapter':'codex','kinds':['memory','rule','skill'],'ids':{'memory':'*','rule':['synthetic-rule'],'skill':['synthetic-skill']},'dependencies':{'commands':[],'python_modules':[]}}

class Cloud:
    def __init__(self):
        self.grant={'scopes':['read','put'],'artifact_types':POLICY['kinds'],'ids':POLICY['ids']}
        self.records={};self.journal={};self.calls=[];self.puts=0;self.offline=False;self.lose_ack=False;self.crash=False
    def request(self,request):
        self.calls.append(copy.deepcopy(request))
        if self.offline:raise OSError('Synthetic disconnected')
        method=request['method'];params=request['params'];rid=request['request_id'];contract.validate(method,params)
        if method=='content_catalog':result={'content_api_version':1,'commit':format(self.puts,'040x'),'artifacts':[{'kind':k,'id':i,'revision':contract.envelope(k,i,v)['revision']} for (k,i),v in sorted(self.records.items()) if k in params['kinds']]}
        elif method=='content_read':result=contract.envelope(params['kind'],params['id'],self.records.get((params['kind'],params['id'])))
        else:
            if rid in self.journal:return copy.deepcopy(self.journal[rid])
            key=(params['kind'],params['id']);old=self.records.get(key)
            if params['expected_revision']!=contract.envelope(*key,old)['revision']:return {'request_id':rid,'error':{'code':'revision_conflict','retryable':False},'persistence':{'durable':False}}
            self.records[key]=copy.deepcopy(params['record']);self.puts+=1;result=contract.envelope(*key,self.records[key])
            response={'request_id':rid,'result':result,'persistence':{'durable':True,'state':'remote-confirmed','commit':format(self.puts,'040x')}};self.journal[rid]=copy.deepcopy(response)
            if self.crash:raise SystemExit('Synthetic crash after Server commit')
            if self.lose_ack:self.lose_ack=False;raise OSError('Synthetic lost confirmation')
            return response
        return {'request_id':rid,'result':result,'persistence':{'durable':False,'state':'read-only'}}

class Outbox(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.base=Path(tmp.name).resolve();self.home=self.base/'home';self.home.mkdir();self.cache=self.base/'cache';self.cloud=Cloud();self.adapter=self.restart()
    def restart(self):return ContentAdapter(self.home,self.cache,POLICY,self.cloud,codex_home=self.home/'.codex')
    def request(self,rid='synthetic_write_0001',body=None,revision='missing'):
        return {'request_id':rid,'method':'content_put','params':{'kind':'memory','id':'synthetic-topic','record':dict(MEMORY,body=body or MEMORY['body']),'expected_revision':revision}}
    def plan(self,records=None,fingerprint='a'*64):
        rows=records or [('memory','synthetic-topic',MEMORY),('rule','synthetic-rule',RULE),('skill','synthetic-skill',SKILL)]
        return {'schema':1,'binding':self.adapter.binding,'sources':[{'source_id':'approved:'+kind+':'+identity,'source_fingerprint':fingerprint,'kind':kind,'id':identity,'record':copy.deepcopy(value)} for kind,identity,value in rows],'coverage':[{'source_id':'native-history','status':'unavailable','reason':'Explicitly prohibited; never read'}]}

    def test_write_saved_and_fsynced_before_network_and_restart_reconnect(self):
        self.cloud.offline=True;request=self.request();original=self.cloud.request
        def inspect(value):
            self.assertTrue((self.cache/'outbox'/(request['request_id']+'.json')).is_file());return original(value)
        self.cloud.request=inspect
        with patch('content_cache.os.fsync',wraps=__import__('os').fsync) as fsync:
            reply=self.adapter.invoke(request);self.assertGreaterEqual(fsync.call_count,3)
        self.assertEqual(reply['error']['code'],'content_upload_queued');self.assertFalse(reply['persistence']['durable']);self.assertTrue(reply['local_queue']['saved'])
        self.assertEqual(json.loads((self.cache/'outbox'/(request['request_id']+'.json')).read_bytes())['request'],request)
        self.cloud.request=original;self.cloud.offline=False;restarted=self.restart();self.assertEqual(restarted.refresh()['status'],'content-refreshed');self.assertEqual(restarted.queue_status()['confirmed'],1);self.assertEqual(self.cloud.puts,1)
        self.assertEqual([r for r in self.cloud.calls if r['method']=='content_put'],[request,request]);self.assertTrue(all(p.stat().st_mode&0o077==0 for p in (self.cache/'outbox').iterdir()))

    def test_lost_ack_and_process_crash_replay_original_id_once(self):
        for crash in (False,True):
            with self.subTest(crash=crash):
                self.cloud.lose_ack=not crash;self.cloud.crash=crash;request=self.request('synthetic_crash_'+str(crash),revision=contract.envelope('memory','synthetic-topic',self.cloud.records.get(('memory','synthetic-topic')))['revision'])
                if crash:
                    with self.assertRaises(SystemExit):self.adapter.invoke(request)
                else:self.assertEqual(self.adapter.invoke(request)['local_queue']['state'],'pending')
                self.cloud.crash=False;before=self.cloud.puts;self.adapter=self.restart();self.adapter.refresh();self.assertEqual(self.cloud.puts,before);self.assertEqual(self.adapter.invoke(request)['local_queue']['state'],'confirmed')

    def test_actual_subprocess_exit_keeps_request_for_next_process(self):
        code="import sys;sys.path.insert(0,sys.argv[1]);from test_content_outbox import *;c=Cloud();c.offline=True;a=ContentAdapter(sys.argv[2],sys.argv[3],POLICY,c,codex_home=Path(sys.argv[2])/'.codex');a.invoke("+repr(self.request())+");raise SystemExit(23)"
        run=subprocess.run([sys.executable,'-I','-B','-c',code,str(ROOT/'tests'),str(self.home),str(self.cache)],capture_output=True,text=True);self.assertEqual(run.returncode,23,run.stderr)
        self.assertEqual(self.restart().queue_status()['pending'],1);self.restart().refresh();self.assertEqual(self.cloud.puts,1)

    def test_same_id_cannot_change_request_or_cas_base(self):
        self.cloud.offline=True;self.adapter.invoke(self.request());before=(self.cache/'outbox/synthetic_write_0001.json').read_bytes()
        with self.assertRaises(CacheBlocked):self.adapter.invoke(self.request(body='Changed content'))
        with self.assertRaises(CacheBlocked):self.adapter.invoke(self.request(revision='b'*64))
        self.assertEqual((self.cache/'outbox/synthetic_write_0001.json').read_bytes(),before)

    def test_process_killed_before_confirmation_rename_keeps_original_replayable(self):
        self.cloud.offline=True;self.adapter.invoke(self.request())
        code=("import os,sys\nsys.path.insert(0,sys.argv[1])\nfrom test_content_outbox import *\nc=Cloud();a=ContentAdapter(sys.argv[2],sys.argv[3],POLICY,c,codex_home=Path(sys.argv[2])/'.codex')\noriginal=os.replace\ndef killed(source,target):\n if str(target).endswith('/outbox/synthetic_write_0001.json'):os._exit(37)\n return original(source,target)\nos.replace=killed\na.refresh()\n")
        run=subprocess.run([sys.executable,'-I','-B','-c',code,str(ROOT/'tests'),str(self.home),str(self.cache)],capture_output=True,text=True);self.assertEqual(run.returncode,37,run.stderr)
        orphans=list((self.cache/'outbox').glob('.client-native-*'));self.assertTrue(orphans)
        self.assertEqual(self.restart().queue_status()['pending'],1);self.cloud.offline=False;self.assertEqual(self.restart().refresh()['status'],'content-refreshed');self.assertEqual(self.cloud.puts,1);self.assertTrue(all(p.exists() for p in orphans))

    def test_oversized_ledger_and_failed_durable_save_preserve_state_without_network(self):
        self.adapter.stage_import(self.plan());before=(self.cache/'imports.json').read_bytes()
        with self.assertRaises(CacheBlocked):self.adapter._save('imports.json',{'oversized':'x'*1048576})
        self.assertEqual((self.cache/'imports.json').read_bytes(),before)
        with patch('content_cache.os.fsync',side_effect=OSError('Synthetic durable save failure')):
            with self.assertRaises(OSError):self.adapter.invoke(self.request())
        self.assertEqual(self.cloud.calls,[])

    def test_inventory_entry_capacity_preserves_previous_readable_ledger(self):
        for batch in range(4):
            plan=self.plan([('memory','synthetic-topic',MEMORY)]);source=plan['sources'][0]
            plan['sources']=[dict(source,source_id='approved:source:'+str(batch*256+i)) for i in range(256)];self.adapter.stage_import(plan)
        before=(self.cache/'imports.json').read_bytes()
        with self.assertRaises(CacheBlocked):self.adapter.stage_import(self.plan([('memory','synthetic-topic',MEMORY)]))
        self.assertEqual((self.cache/'imports.json').read_bytes(),before);self.assertEqual(self.adapter.import_status()['counts'],{'inventoried':1024});self.assertEqual(self.cloud.calls,[])

    def test_missing_or_wrong_confirmation_never_completes(self):
        original=self.cloud.request
        for change in ('rid','durable','state','commit','record','revision'):
            def uncertain(r):
                value=original(r)
                if r['method']=='content_put':
                    if change=='rid':value['request_id']='wrong_request_id'
                    elif change=='durable':value['persistence']['durable']=False
                    elif change=='state':value['persistence']['state']='local-only'
                    elif change=='commit':value['persistence']['commit']='invalid'
                    elif change=='record':value['result']['record']=dict(MEMORY,body='Unrelated acknowledged body')
                    else:value['result']['revision']='invalid'
                return value
            self.cloud.request=uncertain;self.assertEqual(self.adapter.invoke(self.request())['local_queue']['state'],'pending')
        self.cloud.request=original;self.adapter.refresh();self.assertEqual(self.cloud.puts,1);self.assertEqual(self.adapter.queue_status()['confirmed'],1)

    def test_concurrent_cas_retains_both_and_never_rebases(self):
        first=self.request();second=self.request('synthetic_write_0002',body='Other client content');barrier=threading.Barrier(2);results=[]
        def write(request):barrier.wait();results.append(self.restart().invoke(request))
        workers=[threading.Thread(target=write,args=(r,)) for r in (first,second)]
        for w in workers:w.start()
        for w in workers:w.join()
        self.assertEqual(len(results),2);self.assertEqual({v['local_queue']['state'] for v in results},{'confirmed','conflict'});self.assertEqual(self.cloud.puts,1)
        conflict=next(r for r in self.adapter._queued() if r['state']=='conflict');self.assertNotEqual(conflict['request']['params']['record'],conflict['remote']['record']);self.assertEqual(conflict['request']['params']['expected_revision'],'missing')
        before=len([r for r in self.cloud.calls if r['method']=='content_put']);self.restart().refresh();self.assertEqual(len([r for r in self.cloud.calls if r['method']=='content_put']),before)

    def test_same_id_concurrent_retry_only_uploads_once(self):
        results=[];workers=[threading.Thread(target=lambda:results.append(self.restart().invoke(self.request()))) for _ in range(2)]
        for w in workers:w.start()
        for w in workers:w.join()
        self.assertEqual(len(results),2);self.assertEqual(self.cloud.puts,1);self.assertEqual(self.adapter.queue_status()['confirmed'],1)

    def test_pending_protects_mapping_and_hand_edits_are_preserved(self):
        self.cloud.records[('memory','synthetic-topic')]=copy.deepcopy(MEMORY);self.adapter.refresh();path=self.home/'.config/agent-brain-client/content/memory/synthetic-topic.md';path.write_text('User hand edit');self.cloud.offline=True
        revision=contract.envelope('memory','synthetic-topic',MEMORY)['revision'];self.adapter.invoke(self.request(body='Approved queued edit',revision=revision));self.assertEqual(path.read_text(),'User hand edit')
        self.cloud.offline=False;self.assertEqual(self.restart().refresh()['status'],'content-refresh-blocked');self.assertEqual(path.read_text(),'User hand edit');self.assertEqual(self.cloud.records[('memory','synthetic-topic')]['body'],'Approved queued edit')

    def test_first_repeat_import_offline_inventory_mapping_no_enable(self):
        plan=self.plan();self.cloud.offline=True;self.assertFalse(self.adapter.stage_import(plan)['data_transferred']);self.assertEqual(self.adapter.refresh()['status'],'content-local-changes-retained');self.assertEqual(self.cloud.puts,0);self.assertFalse((self.home/'.codex/AGENTS.md').exists())
        self.cloud.offline=False;self.adapter=self.restart();self.assertEqual(self.adapter.refresh()['status'],'content-refreshed');self.assertEqual(self.cloud.puts,3);self.assertEqual(self.adapter.import_status()['counts'],{'imported':3});self.assertFalse((self.home/'.agents/skills/synthetic-skill').exists())
        self.adapter.stage_import(plan);self.adapter.refresh();self.assertEqual(self.cloud.puts,3);self.assertFalse(self.adapter.import_status()['account_complete'])

    def test_duplicates_and_existing_rule_skill_enablement_untouched(self):
        for kind,identity,value in [('memory','synthetic-topic',MEMORY),('rule','synthetic-rule',dict(RULE,enabled=True)),('skill','synthetic-skill',dict(SKILL,enabled=True))]:self.cloud.records[(kind,identity)]=copy.deepcopy(value)
        self.adapter.stage_import(self.plan());self.adapter.refresh();self.assertEqual(self.cloud.puts,0);self.assertEqual(self.adapter.import_status()['counts'],{'duplicate':3})
        self.assertTrue(self.cloud.records[('skill','synthetic-skill')]['enabled']);self.assertTrue(self.cloud.records[('rule','synthetic-rule')]['enabled'])
        changed=dict(RULE,body='Changed source rule');self.adapter.stage_import(self.plan([('rule','synthetic-rule',changed)],'b'*64));self.adapter.refresh();self.assertEqual(self.cloud.records[('rule','synthetic-rule')],dict(changed,enabled=True))

    def test_first_import_never_restores_forgotten_server_memory(self):
        original=self.cloud.request
        def tombstone(request):
            value=original(request)
            if request['method']=='content_read':value['result']['revision']='b'*64
            return value
        self.cloud.request=tombstone;self.adapter.stage_import(self.plan([('memory','synthetic-topic',MEMORY)]));self.assertEqual(self.adapter.refresh()['status'],'content-local-changes-retained');self.assertEqual(self.adapter.import_status()['counts'],{'conflict':1});self.assertEqual(self.cloud.puts,0)

    def test_conflict_remote_read_disconnect_recovers_both_versions(self):
        self.adapter.invoke(self.request());original=self.cloud.request
        def failed_read(request):
            if request['method']=='content_read':raise OSError('Synthetic conflict read disconnect')
            return original(request)
        self.cloud.request=failed_read;conflicted=self.request('synthetic_read_disconnect',body='Local conflicting body');self.assertEqual(self.adapter.invoke(conflicted)['local_queue']['state'],'conflict');self.assertIsNone(self.adapter._load('outbox/'+conflicted['request_id']+'.json')['remote'])
        self.cloud.request=original;self.restart().refresh();row=self.adapter._load('outbox/'+conflicted['request_id']+'.json');self.assertEqual(row['remote']['record'],MEMORY);self.assertEqual(row['request'],conflicted);self.assertEqual(self.cloud.puts,1)

    def test_incremental_known_base_and_concurrent_remote_conflicts(self):
        self.adapter.stage_import(self.plan());self.adapter.refresh();changed=dict(MEMORY,body='Updated source');self.adapter.stage_import(self.plan([('memory','synthetic-topic',changed)],'b'*64));self.adapter.refresh();self.assertEqual(self.cloud.puts,4);self.assertEqual(self.cloud.records[('memory','synthetic-topic')],changed)
        self.cloud.records[('memory','synthetic-topic')]=dict(changed,body='Other writer update');next_value=dict(changed,body='New local version');self.adapter.stage_import(self.plan([('memory','synthetic-topic',next_value)],'c'*64));self.assertEqual(self.adapter.refresh()['status'],'content-local-changes-retained');self.assertEqual(self.cloud.puts,4)
        conflict=next(v for v in self.adapter._imports()['items'].values() if v['status']=='conflict');self.assertEqual(conflict['source']['record'],next_value);self.assertEqual(conflict['remote']['record']['body'],'Other writer update')

    def test_import_fingerprint_scope_activation_and_binding_drift_block(self):
        plan=self.plan();self.adapter.stage_import(plan);changed=copy.deepcopy(plan);changed['sources'][0]['record']['body']='Changed without fingerprint'
        with self.assertRaises(CacheBlocked):self.adapter.stage_import(changed)
        changed=copy.deepcopy(plan);changed['binding']['codex_home']=str(self.base/'other')
        with self.assertRaises(CacheBlocked):self.adapter.stage_import(changed)
        for index in (1,2):
            changed=copy.deepcopy(plan);changed['sources'][index]['record']['enabled']=True
            with self.assertRaises(CacheBlocked):self.adapter.stage_import(changed)
        changed=copy.deepcopy(plan);changed['sources'][1]['id']='unapproved-rule'
        with self.assertRaises(CacheBlocked):self.adapter.stage_import(changed)
        self.assertEqual(self.cloud.puts,0)

    def test_explicit_merge_preserves_evidence_and_recovers_lost_ack(self):
        self.adapter.invoke(self.request());conflicted=self.request('synthetic_conflict_001',body='Local conflicting version');self.assertEqual(self.adapter.invoke(conflicted)['local_queue']['state'],'conflict')
        merged=self.request('synthetic_merged_0001',body='Explicit merged version',revision=contract.envelope('memory','synthetic-topic',self.cloud.records[('memory','synthetic-topic')])['revision']);refs={'request_ids':[conflicted['request_id']],'source_keys':[]};self.cloud.lose_ack=True
        self.assertEqual(self.adapter.invoke(merged,reconciles=refs)['local_queue']['state'],'pending');self.assertEqual(self.adapter.queue_status()['conflict'],1)
        self.restart().refresh();old=self.adapter._load('outbox/'+conflicted['request_id']+'.json');self.assertEqual(old['state'],'reconciled');self.assertEqual(old['request'],conflicted);self.assertEqual(old['remote']['record'],MEMORY);self.assertEqual(old['resolution_request_id'],merged['request_id']);self.assertEqual(self.cloud.puts,2);self.assertTrue(self.adapter.invoke(merged,reconciles=refs)['persistence']['durable'])

    def test_source_conflict_merge_and_local_mcp_metadata_not_forwarded(self):
        from content_mcp import ContentServer
        self.cloud.records[('memory','synthetic-topic')]=dict(MEMORY,body='Other remote content');self.adapter.stage_import(self.plan([('memory','synthetic-topic',MEMORY)]));self.adapter.refresh();key=self.adapter.import_status()['conflicts'][0]['source_key']
        request=self.request('synthetic_source_merge',body='Merged source and remote',revision=contract.envelope('memory','synthetic-topic',self.cloud.records[('memory','synthetic-topic')])['revision'])
        reply=ContentServer(self.adapter).invoke('content_put',dict(request['params'],request_id=request['request_id'],reconciles={'request_ids':[],'source_keys':[key]}));self.assertFalse(reply['isError'],reply);self.assertEqual(self.adapter._imports()['items'][key]['status'],'reconciled');self.assertEqual(next(r for r in self.cloud.calls if r['method']=='content_put'),request);self.assertEqual(self.adapter.refresh()['status'],'content-current')

    def test_service_principal_switch_preserves_queue_without_replay(self):
        self.cloud.offline=True;self.adapter.invoke(self.request());before=(self.cache/'outbox/synthetic_write_0001.json').read_bytes();self.cloud.grant=dict(self.cloud.grant,actor='another-principal');self.cloud.offline=False
        self.assertEqual(self.restart().refresh()['status'],'content-refresh-blocked');self.assertEqual(self.cloud.puts,0);self.assertEqual((self.cache/'outbox/synthetic_write_0001.json').read_bytes(),before)

if __name__=='__main__':unittest.main()
