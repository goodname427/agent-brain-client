"""Run the fixed 20/10 synthetic acceptance against actual Server/Core code.

This is an explicit cross-repository check. It requires a local Server source
checkout with its test fixtures; all Server state, credentials, and source
files are synthetic and created in temporary directories.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

CLIENT_ROOT=Path(__file__).resolve().parents[1]
SERVER_COMMIT='ce48ec0cb3752899a41e99abf02b4facca1d6708'


def run(server_root):
    server_root=Path(server_root).expanduser()
    if server_root.is_symlink() or not (server_root/'tests/test_content_service.py').is_file():
        raise ValueError('A local AgentBrain Server source checkout with its synthetic test fixture is required.')
    server_root=server_root.resolve()
    commit=subprocess.run(['git','-C',str(server_root),'rev-parse','HEAD'],check=True,capture_output=True,text=True).stdout.strip()
    dirty=subprocess.run(['git','-C',str(server_root),'status','--porcelain'],check=True,capture_output=True,text=True).stdout
    if commit!=SERVER_COMMIT or dirty:
        raise ValueError('The Server checkout must be clean and pinned to '+SERVER_COMMIT+'.')
    sys.path.insert(0,str(server_root/'tests'))
    sys.path.insert(0,str(server_root/'scripts'))
    from test_content_service import ContentService
    sys.path.insert(0,str(CLIENT_ROOT/'scripts'))
    sys.path.insert(0,str(CLIENT_ROOT/'adapters/codex'))
    sys.modules.pop('brain_lock',None)
    from content_cache import ContentAdapter

    fixture=json.loads((CLIENT_ROOT/'tests/fixtures/agentbrain_030_acceptance.json').read_text())
    case=ContentService('test_typed_memory_reuses_canonical_core_and_one_journal')
    case.setUp()
    try:
        with tempfile.TemporaryDirectory(prefix='agentbrain030-server-e2e-') as tmp:
            base=Path(tmp);home=base/'home';home.mkdir();cache=base/'cache'
            memories=home/'.codex/memories';memories.mkdir(parents=True)
            for record in fixture['records']:
                (memories/(record['id']+'.md')).write_text(record['body'],encoding='utf-8')
            policy={'schema':1,'adapter':'codex','kinds':['memory'],'ids':{'memory':'*'},
                    'dependencies':{'commands':[],'python_modules':[]}}

            class IsolatedServerBridge:
                grant={'scopes':['read','put'],'artifact_types':['memory'],'ids':{'memory':'*'},'platform':'darwin-arm64'}
                queue_identity={'service':'isolated-real-server-acceptance','principal':'owner-adapter'}
                def request(self,request):
                    status,response=case.api.call(request['method'],request['params'],token=case.token,rid=request['request_id'])
                    if status>=400:raise AssertionError((request['method'],status,response))
                    return response

            adapter=ContentAdapter(home,cache,policy,IsolatedServerBridge(),codex_home=home/'.codex')
            rows=adapter.inventory_memory_sources()['sources'];indexed={row['source_id']:row for row in rows}
            if len(rows)!=20:raise AssertionError('Expected exactly 20 synthetic persisted memories.')
            for record in fixture['records']:
                source_id='codex-memory:'+hashlib.sha256((record['id']+'.md').encode()).hexdigest()
                source=indexed[source_id]
                adapter.read_memory_source(source_id,source['source_fingerprint'])
                adapter.review_memory_source(source_id,source['source_fingerprint'],'shareable','safe-cross-project-personal-summary')
                typed={key:record[key] for key in ('title','description','body','type')}
                adapter.stage_memory_import(source_id,source['source_fingerprint'],'memory',record['id'],typed)
            if adapter.refresh()['status']!='content-refreshed':raise AssertionError('Actual Server import did not refresh.')
            if adapter.import_status()['counts']!={'imported':20}:raise AssertionError('Actual Server did not durably confirm all 20 imports.')
            before_rerun=case.api.head()
            for record in fixture['records']:
                source_id='codex-memory:'+hashlib.sha256((record['id']+'.md').encode()).hexdigest()
                source=indexed[source_id];typed={key:record[key] for key in ('title','description','body','type')}
                adapter.stage_memory_import(source_id,source['source_fingerprint'],'memory',record['id'],typed)
            adapter.refresh()
            if case.api.head()!=before_rerun:raise AssertionError('Idempotent rerun created a new Server commit.')

            correct=0
            for index,item in enumerate(fixture['recall']):
                result=adapter.invoke({'request_id':'verify_'+hashlib.sha256(item['question'].encode()).hexdigest()[:32],
                    'method':'memory_query','params':{'kind':'memory','query':item['question'],'since':None,'until':None,'limit':3}})
                topics=result['result']['topics'];match=next((topic for topic in topics if topic['id']==item['id']),None)
                if match and item['evidence'].lower() in match['preview'].lower():correct+=1
            if correct<9:raise AssertionError(f'Actual Server recalled only {correct}/10 evidence questions.')

            target='exp-02';read=case.call('content_read',{'kind':'memory','id':target})[1]['result']
            status,forgot=case.call('content_remove',{'kind':'memory','id':target,'expected_revision':read['revision']})
            if status!=200 or forgot['persistence']['durable'] is not True:raise AssertionError('Actual Server content_remove was not durable.')
            changed=next(record for record in fixture['records'] if record['id']==target)
            source_file=memories/(target+'.md');source_file.write_text('Changed synthetic source after explicit forget.',encoding='utf-8')
            refreshed=adapter.inventory_memory_sources()['sources']
            new_fingerprint=hashlib.sha256(source_file.read_bytes()).hexdigest()
            source=next(row for row in refreshed if row['source_id']=='codex-memory:'+hashlib.sha256((target+'.md').encode()).hexdigest() and row['source_fingerprint']==new_fingerprint)
            adapter.read_memory_source(source['source_id'],source['source_fingerprint'])
            adapter.review_memory_source(source['source_id'],source['source_fingerprint'],'shareable','safe-cross-project-personal-summary')
            changed_record={'title':changed['title'],'description':changed['description'],
                            'body':'A changed synthetic fact after explicit forget.','type':changed['type']}
            adapter.stage_memory_import(source['source_id'],source['source_fingerprint'],'memory',target,changed_record)
            if adapter.refresh()['status']!='content-local-changes-retained':raise AssertionError('Actual Server tombstone did not block re-import.')
            if adapter.import_status()['counts']!={'conflict':1,'imported':20}:raise AssertionError('Tombstone conflict was not retained for review.')
            return {'status':'passed','server':'actual isolated Server Service + Core','imported':20,
                    'idempotent_rerun':True,'recall_correct':correct,'recall_questions':10,
                    'real_content_remove_tombstone_blocks_reimport':True,'server_commit':commit,
                    'production_data_accessed':False}
    finally:
        case.doCleanups()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-root',type=Path,required=True,help='Local Server source checkout; its synthetic test fixture is used.')
    args=parser.parse_args()
    print(json.dumps(run(args.server_root),ensure_ascii=False))


if __name__=='__main__':main()
