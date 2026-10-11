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
        validate_policy(policy,getattr(client,'grant',None))
        if self.home==self.cache or self.cache in self.home.parents:raise CacheBlocked()
        if any(self.cache==self.home/name or self.home/name in self.cache.parents for name in ('.codex','.agents','.config/agent-brain-client/content')):raise CacheBlocked()
        self._safe(self.home,self.home);self._safe(self.cache,self.cache)
        self.cache.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.cache.stat().st_mode&0o077:raise CacheBlocked()

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
        path=self.cache/name;self._safe(path,self.cache);atomic(path,contract.encode(value),0o600)

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
                self._recover();owned=self._owned();snapshot=self._fetch();desired=self._mapping(snapshot,owned)
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

    def invoke(self,request):
        method=request.get('method');params=request.get('params');contract.validate(method,params)
        kinds=params['kinds'] if method=='content_catalog' else [params['kind']]
        if any(kind not in self.policy['kinds'] for kind in kinds) or method!='content_catalog' and not self._allows(params['kind'],params['id']):raise CacheBlocked()
        self.refresh()  # Startup also invokes refresh; each tool boundary checks cloud changes.
        result=self.client.request(request)
        if method in contract.WRITES and 'error' not in result and result.get('persistence',{}).get('durable') is not True:raise CacheBlocked()
        if method=='content_catalog' and 'error' not in result:
            value=result.get('result',{})
            if not isinstance(value.get('artifacts'),list):raise CacheBlocked()
            visible=[]
            for item in value['artifacts']:
                if not isinstance(item,dict) or set(item)!={'kind','id','revision'}:raise CacheBlocked()
                contract.validate('content_read',{'kind':item['kind'],'id':item['id']})
                if item['kind'] in kinds and self._allows(item['kind'],item['id']):visible.append(item)
            result=dict(result,result=dict(value,artifacts=visible))
        return dict(result,local_refresh=self.refresh())
