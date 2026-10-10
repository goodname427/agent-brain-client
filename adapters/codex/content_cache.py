"""Opt-in Codex content cache/native mapping; no Core, Git or secret discovery.

The embedding approved transport owns authentication. Refresh at startup/tool
boundaries is automatic when this candidate is wired; no new daemon is created.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import uuid
BASE=Path(__file__).resolve().parent
ROOT=BASE.parent if (BASE.parent/'scripts').is_dir() else BASE.parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
try:
    import content_contract as contract
except ImportError:
    import brain_content as contract  # Same neutral schema in the development tree.
from codex_connect import atomic
from brain_lock import locked

BEGIN='<!-- agent-brain-content:begin -->';END='<!-- agent-brain-content:end -->'
BLOCK=re.compile(re.escape(BEGIN)+r'.*?'+re.escape(END)+r'(?:\r?\n)?',re.S)

class CacheBlocked(Exception):pass
def sha(raw):return hashlib.sha256(raw).hexdigest()

def validate_policy(policy,grant=None):
    if not isinstance(policy,dict) or set(policy)!={'schema','adapter','kinds','ids','dependencies'} or policy['schema']!=1 or policy['adapter']!='codex':raise CacheBlocked()
    contract.validate('content_catalog',{'kinds':policy['kinds']})
    if not isinstance(policy['ids'],dict) or set(policy['ids'])!=set(policy['kinds']):raise CacheBlocked()
    for kind,ids in policy['ids'].items():
        if kind=='memory' and ids=='*':continue
        if not isinstance(ids,list) or len(set(ids))!=len(ids):raise CacheBlocked()
        for identity in ids:contract.validate('content_read',{'kind':kind,'id':identity})
    if not isinstance(policy['dependencies'],dict) or set(policy['dependencies'])!={'commands','python_modules'}:raise CacheBlocked()
    contract.strings(policy['dependencies']['commands']);contract.strings(policy['dependencies']['python_modules'])
    if grant and ('read' not in grant['scopes'] or set(policy['kinds'])-set(grant['artifact_types'])):raise CacheBlocked()
    if grant:
        try:contract.grant_ids(grant['artifact_types'],grant.get('ids'))
        except ValueError:raise CacheBlocked()
        for kind,ids in policy['ids'].items():
            if ids=='*' and grant['ids'].get(kind)!='*' or ids!='*' and any(not contract.allows(grant['ids'],kind,identity) for identity in ids):raise CacheBlocked()

class ContentAdapter:
    def __init__(self,home,cache,policy,client,health=None,codex_home=None):
        if Path(home).is_symlink() or Path(cache).is_symlink():raise CacheBlocked()
        self.home,self.cache=Path(home).resolve(),Path(cache).resolve();self.client=client
        native=Path(codex_home or os.environ.get('CODEX_HOME',str(self.home/'.codex'))).expanduser()
        if not native.is_absolute() or native.is_symlink():raise CacheBlocked()
        self.native_home=native.resolve();self.binding={'device_home':str(self.home),'codex_home':str(self.native_home)}
        self.policy=policy;self.policy_sha=sha(contract.encode({'policy':policy,'binding':self.binding}));self.health=health or (lambda:None)
        # Bind pending writes to a service/principal, independently of source/data Git.
        self.queue_sha=sha(contract.encode({'policy_sha':self.policy_sha,'service':getattr(client,'queue_identity',getattr(client,'grant',None))}))
        validate_policy(policy,getattr(client,'grant',None))
        if self.home==self.cache or self.cache in self.home.parents:raise CacheBlocked()
        if any(self.cache==self.home/name or self.home/name in self.cache.parents for name in ('.codex','.agents','.config/agent-brain-client/content')):raise CacheBlocked()
        self._safe(self.home,self.home);self._safe(self.cache,self.cache)
        self.cache.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.cache.stat().st_mode&0o077:raise CacheBlocked()
        fd=os.open(self.cache.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)

    def _safe(self,path,base):
        if path!=base and base not in path.parents:raise CacheBlocked()
        for item in [path,*path.parents]:
            if item.is_symlink():raise CacheBlocked()
            if item==base:break

    def _path(self,name):
        if name=='.codex/AGENTS.md':
            path=self.native_home/'AGENTS.md';self._safe(path,self.native_home);return path
        else:
            matched=re.fullmatch(r'\.config/agent-brain-client/content/(memory|rule)/([a-z0-9-]+)\.md',name)
            skill=re.fullmatch(r'\.agents/skills/([a-z0-9-]+)/(.+)',name)
            if matched:
                contract.validate('content_read',{'kind':matched[1],'id':matched[2]})
                if not self._allows(matched[1],matched[2]):raise CacheBlocked()
            elif skill:
                if skill[1] not in self.policy['ids'].get('skill',[]):raise CacheBlocked()
                contract.skill_path(skill[2])
            else:raise CacheBlocked()
        path=self.home/name;self._safe(path,self.home);return path

    def _allows(self,kind,identity):
        ids=self.policy['ids'].get(kind,[])
        return kind=='memory' and ids=='*' or identity in ids

    def methods(self):
        grant=getattr(self.client,'grant',{'scopes':['read','write'],'artifact_types':self.policy['kinds']})
        return contract.methods(grant['scopes']) if set(self.policy['kinds'])<=set(grant['artifact_types']) else set()

    def _load(self,name,default=None):
        path=self.cache/name;self._safe(path,self.cache)
        if not path.exists():return default
        if not path.is_file() or path.stat().st_mode&0o077 or path.stat().st_size>1048576:raise CacheBlocked()
        return json.loads(path.read_text())

    def _save(self,name,value):
        raw=contract.encode(value)
        if len(raw)>1048576:raise CacheBlocked()
        path=self.cache/name;self._safe(path,self.cache);atomic(path,raw,0o600)
        for directory in [path.parent,*path.parent.parents]:
            fd=os.open(directory,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
            if directory==self.cache:break

    def _check_write(self,request):
        if not isinstance(request,dict) or set(request)!={'request_id','method','params'}:raise CacheBlocked()
        rid=request['request_id'];method=request['method'];params=request['params']
        if not isinstance(rid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',rid) or method not in contract.WRITES or method not in self.methods():raise CacheBlocked()
        contract.validate(method,params)
        if params['kind'] not in self.policy['kinds'] or not self._allows(params['kind'],params['id']):raise CacheBlocked()

    def _queued(self):
        directory=self.cache/'outbox';self._safe(directory,self.cache)
        if directory.exists() and (not directory.is_dir() or directory.stat().st_mode&0o077):raise CacheBlocked()
        paths=list(directory.iterdir()) if directory.exists() else []
        if len(paths)>2048:raise CacheBlocked()
        rows=[]
        for path in paths:
            self._safe(path,self.cache)
            # Preserve temporary evidence left by killed atomic saves; replay only
            # renamed request files so a lost confirmation remains recoverable.
            if re.fullmatch(r'\.client-native-[A-Za-z0-9_-]+',path.name):
                if not path.is_file() or path.stat().st_mode&0o077 or path.stat().st_size>1048576:raise CacheBlocked()
                continue
            if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}\.json',path.name):raise CacheBlocked()
            row=self._load('outbox/'+path.name)
            if not isinstance(row,dict) or set(row)!={'schema','policy_sha','created','request','request_sha','state','response','remote','reconciles','resolution_request_id'} or row['schema']!=1 or row['policy_sha']!=self.queue_sha or type(row['created'])!=int or row['state'] not in ('pending','confirmed','conflict','blocked','reconciled'):raise CacheBlocked()
            self._check_write(row['request'])
            if row['request']['request_id']+'.json'!=path.name or sha(contract.encode(row['request']))!=row['request_sha']:raise CacheBlocked()
            rows.append(row)
        if len(rows)>1024:raise CacheBlocked()
        return sorted(rows,key=lambda row:(row['created'],row['request']['request_id']))

    def _enqueue(self,request,reconciles=None):
        self._check_write(request)
        if reconciles is None:reconciles={'request_ids':[],'source_keys':[]}
        if not isinstance(reconciles,dict) or set(reconciles)!={'request_ids','source_keys'}:raise CacheBlocked()
        for field,pattern in [('request_ids',r'[A-Za-z0-9_-]{16,128}'),('source_keys',r'[a-f0-9]{64}')]:
            values=reconciles[field]
            if not isinstance(values,list) or len(values)>256 or any(not isinstance(v,str) or not re.fullmatch(pattern,v) for v in values) or len(set(values))!=len(values):raise CacheBlocked()
        request=json.loads(contract.encode(request));digest=sha(contract.encode(request))
        name='outbox/'+request['request_id']+'.json';old=self._load(name)
        if old is not None:
            if old['request_sha']!=digest or old['request']!=request or old['reconciles']!=reconciles:raise CacheBlocked()
            self._queued();return old
        rows=self._queued()
        if len(rows)>=1024:raise CacheBlocked()
        target=(request['params']['kind'],request['params']['id'])
        for rid in reconciles['request_ids']:
            previous=next((r for r in rows if r['request']['request_id']==rid),None)
            if previous is None or previous['state']!='conflict' or (previous['request']['params']['kind'],previous['request']['params']['id'])!=target:raise CacheBlocked()
        for key in reconciles['source_keys']:
            item=self._imports()['items'].get(key)
            if item is None or item['status']!='conflict' or (item['source']['kind'],item['source']['id'])!=target:raise CacheBlocked()
        row={'schema':1,'policy_sha':self.queue_sha,'created':time.time_ns(),'request':request,'request_sha':digest,'state':'pending','response':None,'remote':None,'reconciles':reconciles,'resolution_request_id':None}
        self._save(name,row);return row

    def _finish_reconciliation(self,row):
        if row['state']!='confirmed':return
        for rid in row['reconciles']['request_ids']:
            previous=self._load('outbox/'+rid+'.json')
            if previous['state']=='conflict':
                previous.update(state='reconciled',resolution_request_id=row['request']['request_id']);self._save('outbox/'+rid+'.json',previous)
        if row['reconciles']['source_keys']:
            state=self._imports()
            for key in row['reconciles']['source_keys']:
                item=state['items'][key]
                if item['status']=='conflict':item.update(status='reconciled',request_id=row['request']['request_id'])
            self._save('imports.json',state)

    def _drain(self):
        blocked={}
        for row in self._queued():
            request=row['request'];params=request['params'];target=(params['kind'],params['id'])
            if row['state'] in ('conflict','blocked'):
                if row['state']=='conflict' and row['remote'] is None:
                    try:
                        row['remote']=self._read_target(*target);self._save('outbox/'+request['request_id']+'.json',row)
                    except Exception:pass
                blocked.setdefault(target,set()).add(request['request_id']);continue
            if row['state']=='confirmed':self._finish_reconciliation(row);continue
            if row['state']!='pending' or blocked.get(target,set())-set(row['reconciles']['request_ids']):continue
            try:response=self.client.request(json.loads(contract.encode(request)))
            except Exception:break
            if not isinstance(response,dict) or response.get('request_id')!=request['request_id']:break
            if 'error' not in response:
                value=response.get('result');receipt=response.get('persistence')
                if not isinstance(receipt,dict) or receipt.get('durable') is not True or receipt.get('state')!='remote-confirmed' or not isinstance(receipt.get('commit'),str) or not re.fullmatch(r'[a-f0-9]{40}',receipt['commit']):break
                try:self._validate_target(value,*target)
                except Exception:break
                if not self._import_same(params['kind'],params.get('record'),value['record'],preserve_enabled=False):break
                row.update(state='confirmed',response=response)
            else:
                error=response['error']
                if not isinstance(error,dict):break
                if error.get('code')=='revision_conflict':
                    row.update(state='conflict',response=response)
                    try:row['remote']=self._read_target(*target)
                    except Exception:pass
                elif error.get('retryable') is False and error.get('code') not in ('server_proxy_unconfirmed','ssh_proxy_unconfirmed','persistence_pending','pending_write_must_retry'):
                    row.update(state='blocked',response=response)
                else:break
                blocked.setdefault(target,set()).add(request['request_id'])
            self._save('outbox/'+request['request_id']+'.json',row);self._finish_reconciliation(row)

    def queue_status(self):
        rows=self._queued()
        return {state:sum(row['state']==state for row in rows) for state in ('pending','confirmed','conflict','blocked','reconciled')}

    def _queue_result(self,row):
        result=row['response'] if row['state']!='pending' else {'request_id':row['request']['request_id'],'error':{'code':'content_upload_queued','retryable':True},'persistence':{'durable':False}}
        return dict(result,local_queue={'state':row['state'],'saved':True,'request_sha256':row['request_sha']})

    def stage_import(self,plan):
        """Explicit reviewed normalized sources only; no native/file discovery."""
        if not isinstance(plan,dict) or set(plan)!={'schema','binding','sources','coverage'} or plan['schema']!=1 or plan['binding']!=self.binding or not isinstance(plan['sources'],list) or len(plan['sources'])>256:raise CacheBlocked()
        coverage=plan['coverage']
        if not isinstance(coverage,list) or len(coverage)>256:raise CacheBlocked()
        for item in coverage:
            if not isinstance(item,dict) or set(item)!={'source_id','status','reason'} or item['status'] not in ('read','excluded','unavailable','not-checked') or not all(isinstance(item[k],str) and 0<len(item[k])<=1024 for k in item):raise CacheBlocked()
        seen=set()
        for item in plan['sources']:
            self._check_source(item)
            if item['source_id'] in seen:raise CacheBlocked()
            seen.add(item['source_id'])
        with locked(self.cache/'content.lock'):
            state=self._imports()
            for item in plan['sources']:
                key=sha(contract.encode([item['source_id'],item['source_fingerprint']]))
                if key in state['items']:
                    if state['items'][key]['source']!=item:raise CacheBlocked()
                    continue
                state['items'][key]={'source':item,'status':'inventoried','request_id':None,'remote':None}
            if len(state['items'])>1024:raise CacheBlocked()
            state['coverage']=coverage;self._save('imports.json',json.loads(contract.encode(state)))
        return {'status':'content-sources-inventoried','sources':len(plan['sources']),'data_transferred':False,'native_changed':False}

    def _check_source(self,item):
        if not isinstance(item,dict) or set(item)!={'source_id','source_fingerprint','kind','id','record'} or not isinstance(item['source_id'],str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',item['source_id']) or not isinstance(item['source_fingerprint'],str) or not re.fullmatch(r'[a-f0-9]{64}',item['source_fingerprint']):raise CacheBlocked()
        contract.validate('content_put',{'kind':item['kind'],'id':item['id'],'record':item['record'],'expected_revision':'missing'})
        if item['kind'] not in self.policy['kinds'] or not self._allows(item['kind'],item['id']) or item['kind'] in ('rule','skill') and item['record']['enabled']:raise CacheBlocked()

    def _imports(self):
        state=self._load('imports.json',{'schema':1,'policy_sha':self.queue_sha,'items':{},'coverage':[]})
        if not isinstance(state,dict) or set(state)!={'schema','policy_sha','items','coverage'} or state['schema']!=1 or state['policy_sha']!=self.queue_sha or not isinstance(state['items'],dict) or len(state['items'])>1024:raise CacheBlocked()
        for key,item in state['items'].items():
            if not isinstance(item,dict) or set(item)!={'source','status','request_id','remote'} or item['status'] not in ('inventoried','pending','imported','duplicate','conflict','blocked','reconciled'):raise CacheBlocked()
            self._check_source(item['source'])
            if key!=sha(contract.encode([item['source']['source_id'],item['source']['source_fingerprint']])):raise CacheBlocked()
            if item['request_id'] is not None and not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',item['request_id']):raise CacheBlocked()
        return state

    @staticmethod
    def _import_same(kind,left,right,preserve_enabled=True):
        if kind in ('rule','skill') and preserve_enabled and isinstance(left,dict) and isinstance(right,dict):left=dict(left,enabled=right.get('enabled'))
        if kind=='memory' and isinstance(left,dict) and isinstance(right,dict):
            left=dict(left,body=left['body'].replace('\r\n','\n').strip());right=dict(right,body=right['body'].replace('\r\n','\n').strip())
        return left==right

    def _import_pending(self):
        state=self._imports()
        for key,item in state['items'].items():
            source=item['source'];kind,identity=source['kind'],source['id']
            if item['status']=='reconciled':continue
            if item['request_id']:
                row=self._load('outbox/'+item['request_id']+'.json')
                if row is None:raise CacheBlocked()
                item['status']='imported' if row['state']=='confirmed' else row['state'];continue
            if item['status']!='inventoried':continue
            try:remote=self._read_target(kind,identity)
            except Exception:break
            current=remote['record'];desired=source['record']
            if self._import_same(kind,desired,current):item.update(status='duplicate',remote=remote);continue
            if current is None and remote['revision']!='missing':item.update(status='conflict',remote=remote);continue
            if current is not None:
                previous=[v['source']['record'] for k,v in state['items'].items() if k!=key and v['source']['source_id']==source['source_id'] and (v['source']['kind'],v['source']['id'])==(kind,identity) and v['status'] in ('imported','duplicate')]
                if not any(self._import_same(kind,old,current) for old in previous):item.update(status='conflict',remote=remote);continue
                if kind in ('rule','skill'):desired=dict(desired,enabled=current['enabled'])
            params={'kind':kind,'id':identity,'record':desired,'expected_revision':remote['revision']}
            rid='import_'+sha(contract.encode([key,params]));self._enqueue({'request_id':rid,'method':'content_put','params':params})
            item.update(status='pending',request_id=rid,remote=remote)
            self._save('imports.json',state)
        if state['items']:self._save('imports.json',state)

    def import_status(self):
        state=self._imports();counts={}
        for item in state['items'].values():counts[item['status']]=counts.get(item['status'],0)+1
        conflicts=[{'source_key':key,'source_id':item['source']['source_id'],'kind':item['source']['kind'],'id':item['source']['id'],'remote_revision':(item['remote'] or {}).get('revision')} for key,item in state['items'].items() if item['status']=='conflict']
        return {'counts':counts,'conflicts':conflicts,'coverage':state['coverage'],'account_complete':False}

    def _raw(self,name):
        path=self._path(name)
        if not path.exists():return None
        if not path.is_file() or path.stat().st_size>262144:raise CacheBlocked()
        return path.read_bytes()

    def _owned(self):
        owned=self._load('owned.json',{'policy_sha':self.policy_sha,'binding':self.binding,'files':{}})
        if not isinstance(owned,dict) or set(owned)!={'policy_sha','binding','files'} or owned['policy_sha']!=self.policy_sha or owned['binding']!=self.binding or not isinstance(owned['files'],dict):raise CacheBlocked()
        for name,digest in owned['files'].items():
            raw=self._raw(name)
            if raw is None or sha(raw)!=digest:raise CacheBlocked()
        self._skill_dirs(owned['files'],owned['files'])
        return owned

    def _skill_dirs(self,names,owned):
        managed={name.split('/')[2] for name in names if name.startswith('.agents/skills/')}
        for identity in managed:
            directory=self.home/'.agents/skills'/identity;self._safe(directory,self.home)
            for entry in directory.rglob('*'):
                self._safe(entry,self.home)
                if entry.is_dir():continue
                if not entry.is_file() or entry.relative_to(self.home).as_posix() not in owned:raise CacheBlocked()

    def _restore(self,journal):
        # Preflight all files before any restoration; preserve outside edits.
        if set(journal)!={'phase','before','after','owned','active'} or set(journal['before'])!=set(journal['after']) or journal['owned'].get('policy_sha')!=self.policy_sha or journal['owned'].get('binding')!=self.binding:raise CacheBlocked()
        for name,old in journal['before'].items():
            if set(old)!={'data','sha','mode'} or type(old['mode'])!=int or old['mode']&~0o777:raise CacheBlocked()
            backup=base64.b64decode(old['data'],validate=True) if old['data'] is not None else None
            if (sha(backup) if backup is not None else None)!=old['sha']:raise CacheBlocked()
            raw=self._raw(name);digest=sha(raw) if raw is not None else None
            if digest not in (old['sha'],journal['after'][name]):raise CacheBlocked()
        for name,old in journal['before'].items():
            path=self._path(name)
            if old['data'] is None:path.unlink(missing_ok=True)
            else:atomic(path,base64.b64decode(old['data'],validate=True),old['mode'])
        self._save('owned.json',journal['owned']);self._save('active.json',journal['active'])
        self._save('transaction.json',dict(journal,phase='rolled-back'))

    def _recover(self):
        journal=self._load('transaction.json')
        if journal and journal['phase'] not in ('applying','complete','rolled-back'):raise CacheBlocked()
        if journal and journal['phase']=='applying':self._restore(journal)

    def _request(self,method,params):
        response=self.client.request({'request_id':uuid.uuid4().hex,'method':method,'params':params})
        if 'error' in response or not isinstance(response.get('result'),dict):raise CacheBlocked()
        return response['result']

    def _read_target(self,kind,identity):
        value=self._request('content_read',{'kind':kind,'id':identity});self._validate_target(value,kind,identity);return value

    @staticmethod
    def _validate_target(value,kind,identity):
        if not isinstance(value,dict) or set(value)!={'content_api_version','kind','id','revision','record'} or value['content_api_version']!=1 or (value['kind'],value['id'])!=(kind,identity) or not isinstance(value['revision'],str) or not re.fullmatch(r'[a-f0-9]{64}|missing',value['revision']):raise CacheBlocked()
        if value['record'] is not None:contract.record(kind,value['record'],identity)
        # Memory CAS covers canonical Core metadata/forgotten markers, not JSON.
        if kind!='memory' and value!=contract.envelope(kind,identity,value['record']):raise CacheBlocked()

    def _fetch(self):
        kinds=self.policy['kinds'];catalog=self._request('content_catalog',{'kinds':kinds})
        if set(catalog)!={'content_api_version','commit','artifacts'} or catalog['content_api_version']!=1 or not re.fullmatch(r'[a-f0-9]{40}',catalog['commit']) or not isinstance(catalog['artifacts'],list) or len(catalog['artifacts'])>256:raise CacheBlocked()
        records=[];seen=set()
        for item in catalog['artifacts']:
            if not isinstance(item,dict) or set(item)!={'kind','id','revision'}:raise CacheBlocked()
            kind,identity=item['kind'],item['id'];contract.validate('content_read',{'kind':kind,'id':identity})
            if kind not in kinds or (kind,identity) in seen:raise CacheBlocked()
            seen.add((kind,identity))
            # Keep the complete catalog for the final snapshot race check, but
            # never fetch or map content outside this device's selected IDs.
            if not self._allows(kind,identity):continue
            value=self._request('content_read',{'kind':kind,'id':identity})
            if set(value)!={'content_api_version','kind','id','revision','record'} or value['content_api_version']!=1 or value['kind']!=kind or value['id']!=identity or value['revision']!=item['revision'] or not re.fullmatch(r'[a-f0-9]{64}',value['revision']):raise CacheBlocked()
            contract.record(kind,value['record'],identity);records.append(value)
        if self._request('content_catalog',{'kinds':kinds})!=catalog:raise CacheBlocked()
        snapshot={'content_api_version':1,'commit':catalog['commit'],'records':records}
        if len(contract.encode(snapshot))>524288:raise CacheBlocked()
        return snapshot

    def _mapping(self,snapshot,owned):
        desired={};lines=['# Agent Brain cloud content','This mapping is a rebuildable cache. Read/upload through the approved Server API; do not write GitHub directly.','Read at most three relevant memory topics. Read on-demand rules when relevant; conditional rules apply to matching paths. Cloud refresh does not replace already loaded model context.']
        for value in snapshot['records']:
            kind,identity,record=value['kind'],value['id'],value['record']
            if kind in ('memory','rule'):
                name='.config/agent-brain-client/content/'+kind+'/'+identity+'.md';desired[name]=record['body'].encode()
                if kind=='memory':lines.append('Memory '+identity+': '+str(self.home/name))
                elif record['enabled']:
                    trigger=record['trigger']
                    if trigger['mode']=='always':lines.extend(['## '+record['title'],record['body']])
                    else:lines.append('Rule '+identity+' / '+record['title']+' ('+trigger['mode']+', patterns='+json.dumps(trigger['patterns'])+'): '+str(self.home/name))
            elif record['enabled']:
                dependencies=record['dependencies']
                if any(set(dependencies[key])-set(self.policy['dependencies'][key]) for key in dependencies):raise CacheBlocked()
                if any(shutil.which(command) is None for command in dependencies['commands']):raise CacheBlocked()
                # Modules need separately verified dependency evidence; no imports/install.
                if dependencies['python_modules']:raise CacheBlocked()
                contract.skill_frontmatter(record,identity)
                for name,body in record['files'].items():desired['.agents/skills/'+identity+'/'+name]=body.encode()
        block=BEGIN+'\n'+'\n'.join(lines)+'\n'+END+'\n'
        if block.count(BEGIN)!=1 or block.count(END)!=1:raise CacheBlocked()
        name='.codex/AGENTS.md';raw=self._raw(name);text=(raw or b'').decode()
        if text.count(BEGIN)!=text.count(END) or text.count(BEGIN)>1 or BEGIN in text and name not in owned['files']:raise CacheBlocked()
        text=BLOCK.sub('',text)
        desired[name]=(text.rstrip()+'\n\n'+block if text.strip() else block).encode()
        if sum(len(raw) for raw in desired.values())>524288:raise CacheBlocked()
        return desired

    def refresh(self):
        try:
            with locked(self.cache/'content.lock'):
                self._recover();self._import_pending();self._drain();self._import_pending()
                queue=self.queue_status();imports=self.import_status()
                if queue['pending'] or queue['conflict'] or queue['blocked'] or any(imports['counts'].get(v,0) for v in ('inventoried','pending','conflict','blocked')):
                    return {'status':'content-local-changes-retained','native_changed':False,'model_loaded':False,'queue':queue,'imports':imports,'next_action':'Reconnect to replay exact saved requests; explicitly reconcile conflicts before mapping.'}
                owned=self._owned();snapshot=self._fetch();desired=self._mapping(snapshot,owned)
                self._skill_dirs(desired,owned['files'])
                if any(self.cache==self._path(name) or self.cache in self._path(name).parents for name in desired):raise CacheBlocked()
                active=self._load('active.json')
                if active=={'commit':snapshot['commit'],'snapshot_sha':sha(contract.encode(snapshot))} and self._load('snapshots/'+snapshot['commit']+'.json')==snapshot:return {'status':'content-current','native_changed':False,'model_loaded':False}
                before={};after={}
                for name in set(owned['files'])|set(desired):
                    raw=self._raw(name)
                    if name!='.codex/AGENTS.md' and name not in owned['files'] and raw is not None:raise CacheBlocked()
                    path=self._path(name);before[name]={'data':base64.b64encode(raw).decode() if raw is not None else None,'sha':sha(raw) if raw is not None else None,'mode':path.stat().st_mode&0o777 if raw is not None else 0o600}
                    after[name]=sha(desired[name]) if name in desired else None
                journal={'phase':'applying','before':before,'after':after,'owned':owned,'active':active}
                self._save('transaction.json',journal)
                try:
                    for name in before:
                        path=self._path(name)
                        if name in desired:atomic(path,desired[name],before[name]['mode'])
                        else:path.unlink(missing_ok=True)
                    self._save('snapshots/'+snapshot['commit']+'.json',snapshot)
                    self._save('owned.json',{'policy_sha':self.policy_sha,'binding':self.binding,'files':{name:sha(raw) for name,raw in desired.items()}})
                    self._save('active.json',{'commit':snapshot['commit'],'snapshot_sha':sha(contract.encode(snapshot))})
                    self.health();self._owned();self._save('transaction.json',dict(journal,phase='complete'))
                except Exception:self._restore(journal);raise
                return {'status':'content-refreshed','native_changed':True,'model_loaded':False,'commit':snapshot['commit']}
        except Exception:
            try:journal=self._load('transaction.json');uncertain=journal and journal['phase']=='applying'
            except Exception:uncertain=True
            return {'status':'content-recovery-review-required' if uncertain else 'content-refresh-blocked','native_changed':None if uncertain else False,'model_loaded':False,'next_action':'Preserve local files and prior snapshots. Review transport, contract, content scope, dependencies, owned-file drift or interrupted recovery.'}

    def invoke(self,request,reconciles=None):
        method=request.get('method');params=request.get('params');contract.validate(method,params)
        kinds=params['kinds'] if method=='content_catalog' else [params['kind']]
        if any(kind not in self.policy['kinds'] for kind in kinds) or method!='content_catalog' and not self._allows(params['kind'],params['id']):raise CacheBlocked()
        if method in contract.WRITES:
            with locked(self.cache/'content.lock'):
                self._enqueue(request,reconciles)  # Precedes every transport/mapping operation.
                self._drain();result=self._queue_result(self._load('outbox/'+request['request_id']+'.json'))
        else:
            self.refresh();result=self.client.request(request)
        if method=='content_catalog' and 'error' not in result:
            value=result.get('result',{})
            if not isinstance(value.get('artifacts'),list):raise CacheBlocked()
            visible=[]
            for item in value['artifacts']:
                if not isinstance(item,dict) or set(item)!={'kind','id','revision'}:raise CacheBlocked()
                contract.validate('content_read',{'kind':item['kind'],'id':item['id']})
                if item['kind'] in kinds and self._allows(item['kind'],item['id']):visible.append(item)
            result=dict(result,result=dict(value,artifacts=visible))
        refresh=self.refresh() if method not in contract.WRITES or result.get('local_queue',{}).get('state')=='confirmed' else {'status':'content-local-changes-retained','native_changed':False,'model_loaded':False,'queue':self.queue_status()}
        return dict(result,local_refresh=refresh)
