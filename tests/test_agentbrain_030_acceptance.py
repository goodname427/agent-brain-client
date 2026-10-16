"""Fixed synthetic 20-record import and 10-question evidence recall check."""
import hashlib
import json
import re
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
sys.path.insert(0,str(ROOT/'adapters/codex'))
import test_content_outbox as base
from content_mcp import ContentServer

FIXTURE=json.loads((ROOT/'tests/fixtures/agentbrain_030_acceptance.json').read_text())
STOP={'a','an','and','are','did','do','for','how','in','is','it','of','the','to','what','was','were','which','with'}


class RecallCloud(base.Cloud):
    """Deterministic synthetic service implementing documented lexical preview shape."""
    def request(self,request):
        if request.get('method')!='memory_query':
            return super().request(request)
        self.calls.append(request)
        params=request['params'];query=set(re.findall(r'[a-z0-9]+',params['query'].lower()))-STOP
        ranked=[]
        for (kind,identity),record in self.records.items():
            if kind!='memory':continue
            words=set(re.findall(r'[a-z0-9]+',(' '.join(record.values())).lower()))
            score=len(query&words)
            if score:ranked.append((score,identity,record))
        ranked.sort(key=lambda item:(-item[0],item[1]))
        topics=[{'id':identity,'title':record['title'],'description':record['description'],
                 'revision':base.contract.envelope('memory',identity,record)['revision'],
                 'updated_at':'2026-10-15T12:00:00Z','score':score,'preview':record['body'][:320]}
                for score,identity,record in ranked[:params['limit']]]
        return {'request_id':request['request_id'],'result':{'content_api_version':1,'commit':format(self.puts,'040x'),
                'method':'lexical','time_basis':'memory_updated_at','topics':topics},
                'persistence':{'durable':False,'state':'read-only'}}


class AgentBrain030Acceptance(unittest.TestCase):
    def setUp(self):
        self.helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        self.helper.setUp();self.addCleanup(self.helper.doCleanups)
        self.cloud=RecallCloud();self.adapter=base.ContentAdapter(
            self.helper.home,self.helper.cache,base.POLICY,self.cloud,codex_home=self.helper.home/'.codex')
        self.plan={'schema':1,'binding':self.adapter.binding,'sources':[],
                   'coverage':[{'source_id':'synthetic-history','status':'read','reason':'Test fixture only'}]}
        for row in FIXTURE['records']:
            record={key:row[key] for key in ('title','description','body','type')}
            fingerprint=hashlib.sha256((row['id']+'\n'+record['body']).encode()).hexdigest()
            self.plan['sources'].append({'source_id':'synthetic:memory:'+row['id'],'source_fingerprint':fingerprint,
                                         'kind':'memory','id':row['id'],'record':record})

    def test_twenty_experiences_import_restart_and_idempotent_rerun(self):
        self.assertEqual(len(self.plan['sources']),20)
        staged=self.adapter.stage_import(self.plan)
        self.assertEqual(staged['sources'],20)
        self.assertEqual(self.adapter.import_status()['counts'],{'inventoried':20})
        self.assertEqual(self.adapter.refresh()['status'],'content-refreshed')
        self.assertEqual(self.adapter.import_status()['counts'],{'imported':20})
        self.assertEqual(self.cloud.puts,20)
        restarted=base.ContentAdapter(self.helper.home,self.helper.cache,base.POLICY,self.cloud,codex_home=self.helper.home/'.codex')
        restarted.stage_import(self.plan)
        restarted.refresh()
        self.assertEqual(restarted.import_status()['counts'],{'imported':20})
        self.assertEqual(self.cloud.puts,20)
        for row in FIXTURE['records']:
            self.assertEqual(self.cloud.records[('memory',row['id'])]['body'],row['body'])

    def test_ten_queries_return_the_expected_record_with_supporting_evidence(self):
        self.adapter.stage_import(self.plan);self.adapter.refresh()
        server=ContentServer(self.adapter)
        correct=0
        for index,item in enumerate(FIXTURE['recall']):
            with self.subTest(question=item['question']):
                response=server.invoke('memory_query',{'kind':'memory','query':item['question'],'since':None,'until':None,'limit':3})
                self.assertFalse(response['isError'],response)
                topics=response['structuredContent']['result']['topics']
                match=next((topic for topic in topics if topic['id']==item['id']),None)
                if match and item['evidence'].lower() in match['preview'].lower():correct+=1
        self.assertGreaterEqual(correct,9)
        self.assertEqual(correct,10)


if __name__=='__main__':unittest.main()
