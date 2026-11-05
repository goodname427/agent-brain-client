"""Shared content outbox and normalized turn submission, independent of Harness I/O.

Hosts supply storage, locking, target/source validation and native mapping. No
host config, transcript discovery, credentials or context injection lives here.
"""
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

import brain_content as contract
from brain_http import check_request
from brain_lock import require_platform, private_mode
import brain_lock
cache_mode=getattr(brain_lock,'cache_mode',private_mode)


class CacheBlocked(Exception):
    pass


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class ContentOutbox:
    """Existing schema-1 CAS outbox; host-specific methods are supplied by subclasses."""
    cache: Path
    platform: str
    policy: dict[str, Any]
    queue_sha: str
    client: Any
    methods: Callable[..., Any]
    _allows: Callable[..., Any]
    _safe: Callable[..., Any]
    _load: Callable[..., Any]
    _save: Callable[..., Any]
    _assert_sources_shareable: Callable[..., Any]
    _imports: Callable[..., Any]
    _import_same: Callable[..., Any]
    _validate_target: Callable[..., Any]
    _read_target: Callable[..., Any]
    _outbox_lock: Callable[..., Any]
    _outbox_stage: Callable[..., Any]
    _outbox_locked: Callable[..., Any]

    def _check_write(self,request):
        if not isinstance(request,dict) or set(request)!={'request_id','method','params'}:raise CacheBlocked()
        rid=request['request_id'];method=request['method'];params=request['params']
        if not isinstance(rid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',rid) or method not in contract.WRITES or method not in self.methods():raise CacheBlocked()
        contract.validate(method,params)
        if params['kind'] not in self.policy['kinds'] or not self._allows(params['kind'],params['id']):raise CacheBlocked()

    def _queued(self):
        require_platform('cache-storage',self.platform)
        directory=self.cache/'outbox';self._safe(directory,self.cache)
        if directory.exists():cache_mode(directory,directory=True,blocked=CacheBlocked)
        paths=list(directory.iterdir()) if directory.exists() else []
        if len(paths)>2048:raise CacheBlocked()
        rows=[]
        for path in paths:
            self._safe(path,self.cache)
            # Preserve temporary evidence left by killed atomic saves; replay only
            # renamed request files so a lost confirmation remains recoverable.
            if re.fullmatch(r'\.client-native-[A-Za-z0-9_-]+',path.name):
                if not path.is_file() or path.stat().st_size>1048576:raise CacheBlocked()
                cache_mode(path,blocked=CacheBlocked)
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
        # Serialize atomic appends separately from the content transaction,
        # which may wait on remote I/O. Native events can become durable pending
        # rows before that wait; existing rows are only checked, never rewritten.
        path=self.cache/'outbox-enqueue.lock';self._safe(path,self.cache)
        with self._outbox_lock(path,'outbox-lock'):
            with self._outbox_stage('outbox-enqueue'):
                return self._enqueue_locked(request,reconciles)

    def prepare_memory_write(self,request,context):
        """Freeze provenance once, including retries made after a host restart."""
        if 'source_context' in request['params']:return request
        with self._outbox_lock(self.cache/'content.lock','content-lock'):
            old=self._load('outbox/'+request['request_id']+'.json')
        if old is not None:
            context=old['request']['params'].get('source_context')
            if context is None:return request  # Exact legacy replay stays exact.
        return dict(request,params=dict(request['params'],source_context=context))

    def _enqueue_locked(self,request,reconciles=None):
        self._check_write(request)
        if reconciles is None:reconciles={'request_ids':[],'source_keys':[]}
        if not isinstance(reconciles,dict) or set(reconciles)!={'request_ids','source_keys'}:raise CacheBlocked()
        for field,pattern in [('request_ids',r'[A-Za-z0-9_-]{16,128}'),('source_keys',r'[a-f0-9]{64}')]:
            values=reconciles[field]
            if not isinstance(values,list) or len(values)>256 or any(not isinstance(v,str) or not re.fullmatch(pattern,v) for v in values) or len(set(values))!=len(values):raise CacheBlocked()
        # A prior idempotent outbox entry must not bypass a later review revocation.
        if request['method']=='content_set_enabled' and reconciles['source_keys']:raise CacheBlocked()
        self._assert_sources_shareable(reconciles['source_keys'])
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
            if request['method']=='content_set_enabled' and previous['request']['method']!='content_set_enabled':raise CacheBlocked()
        for key in reconciles['source_keys']:
            item=self._imports()['items'].get(key)
            if item is None or item['status'] not in ('conflict','inventoried') or (item['source']['kind'],item['source']['id'])!=target:raise CacheBlocked()
            if item['status']=='inventoried' and not self._import_same(target[0],item['source']['record'],request['params']['record'],preserve_enabled=False):raise CacheBlocked()
        row={'schema':1,'policy_sha':self.queue_sha,'created':time.time_ns(),'request':request,'request_sha':digest,'state':'pending','response':None,'remote':None,'reconciles':reconciles,'resolution_request_id':None}
        self._save(name,row);return row

    def _finish_reconciliation(self,row):
        if row['state']!='confirmed':return False
        resolved=False
        for rid in row['reconciles']['request_ids']:
            previous=self._load('outbox/'+rid+'.json')
            if previous['state']=='conflict':
                previous.update(state='reconciled',resolution_request_id=row['request']['request_id']);self._save('outbox/'+rid+'.json',previous)
                resolved=True
        if row['reconciles']['source_keys']:
            state=self._imports()
            for key in row['reconciles']['source_keys']:
                item=state['items'][key]
                if item['status'] in ('conflict','inventoried'):item.update(status='reconciled',request_id=row['request']['request_id'])
            self._save('imports.json',state)
        return resolved

    @staticmethod
    def _event_allowed(request,event_gate):
        if request['method']!='memory_event_submit' or event_gate is None:return True
        try:return event_gate() is True
        except Exception:return False

    def _drain(self,event_request_id=None,event_gate=None):
        # A resolver can follow older waiters in creation order. Revisit those
        # only after confirmed reconciliation made progress. An uncertain send
        # ends the whole drain, so this never loops over an unconfirmed request.
        while self._drain_pass(event_request_id,event_gate):pass

    def _drain_pass(self,event_request_id=None,event_gate=None):
        resolved=False
        for row in self._queued():
            request=row['request'];params=request['params'];target=(params['kind'],params['id'])
            # Automatic legacy refresh never uploads captured text. An exact
            # Stop retry must pass the current finite hook policy again.
            if request['method']=='memory_event_submit' and request['request_id']!=event_request_id:continue
            if row['state'] in ('conflict','blocked'):
                if row['state']=='conflict' and row['remote'] is None:
                    try:
                        row['remote']=self._read_target(*target);self._save('outbox/'+request['request_id']+'.json',row)
                    except Exception:pass
                # A terminal refusal has no unknown remote outcome. Retain it
                # for review, but let a fresh CAS write proceed independently.
                continue
            if row['state']=='confirmed':
                resolved=self._finish_reconciliation(row) or resolved
                continue
            if row['state']!='pending':continue
            try:self._assert_sources_shareable(row['reconciles']['source_keys'])
            except CacheBlocked:
                row.update(state='blocked',response={'request_id':request['request_id'],'error':{'code':'native_source_review_revoked','retryable':False},'persistence':{'durable':False}})
                self._save('outbox/'+request['request_id']+'.json',row)
                state=self._imports()
                for key in row['reconciles']['source_keys']:
                    if key in state['items'] and state['items'][key]['status'] in ('inventoried','pending'):
                        state['items'][key].update(status='blocked',request_id=request['request_id'])
                self._save('imports.json',state)
                continue
            if not self._event_allowed(request,event_gate):return False
            try:response=self.client.request(json.loads(contract.encode(request)))
            except Exception:return False
            if not isinstance(response,dict) or response.get('request_id')!=request['request_id']:return False
            if 'error' not in response:
                if not self._server_saved(response,request):return False
                row.update(state='confirmed',response=response)
            else:
                error=response['error']
                if not isinstance(error,dict):return False
                if error.get('code')=='revision_conflict':
                    row.update(state='conflict',response=response)
                    try:row['remote']=self._read_target(*target)
                    except Exception:pass
                elif error.get('retryable') is False and error.get('code') not in ('server_proxy_unconfirmed','ssh_proxy_unconfirmed','persistence_pending','pending_write_must_retry'):
                    row.update(state='blocked',response=response)
                else:return False
            with self._outbox_stage('outbox-confirmation' if row['state']=='confirmed' else 'outbox-response'):
                self._save('outbox/'+request['request_id']+'.json',row)
            with self._outbox_stage('outbox-reconciliation'):
                resolved=self._finish_reconciliation(row) or resolved
        return resolved

    def queue_status(self):
        rows=self._queued()
        return {state:sum(row['state']==state for row in rows) for state in ('pending','confirmed','conflict','blocked','reconciled')}

    def inspect_queue(self,*,request_id=None,cursor=None):
        """Read one locked snapshot; never fetch, drain, repair or map content."""
        check_request()
        with self._outbox_stage('queue-inspection'),self._outbox_locked(self.cache/'content.lock',cancel_check=check_request):
            try:rows=self._queued()
            except (ValueError,TypeError,KeyError):raise CacheBlocked() from None
            check_request()
            counts={state:sum(row['state']==state for row in rows) for state in ('pending','confirmed','conflict','blocked','reconciled')}
            base={'counts':counts,'data_transferred':False,'native_changed':False,
                  'remote_snapshot_current':False}
            if request_id is not None:
                row=next((row for row in rows if row['request']['request_id']==request_id),None)
                if row is None:return dict(base,status='not-found',request_id=request_id)
                # Only this exact read exposes bodies; listing contains metadata.
                return dict(base,status='retained',request=row['request'],reconciles=row['reconciles'],
                            response=row['response'],remote_snapshot=row['remote'],
                            local_queue=self._queue_result(row,rows)['local_queue'])
            snapshot=sha(contract.encode(rows));offset=0
            if cursor is not None:
                match=re.fullmatch(r'([a-f0-9]{64}):([0-9]{1,6})',cursor)
                if match is None or match[1]!=snapshot:raise ValueError('Queue changed; restart inspection without cursor.')
                offset=int(match[2])
                if offset>len(rows):raise ValueError('Invalid queue cursor; restart inspection.')
            page=[]
            for row in rows[offset:offset+64]:
                check_request()
                request=row['request'];params=request['params']
                page.append({'request_id':request['request_id'],'method':request['method'],
                             'kind':params['kind'],'id':params['id'],'expected_revision':params.get('expected_revision'),
                             'local_queue':self._queue_result(row,rows)['local_queue']})
            end=offset+len(page)
            return dict(base,status='snapshot',entries=page,
                        next_cursor=f'{snapshot}:{end}' if end<len(rows) else None)

    def _server_saved(self,response,request):
        if not isinstance(response,dict) or response.get('request_id')!=request['request_id'] or 'error' in response:return False
        receipt=response.get('persistence');value=response.get('result');params=request['params']
        if request['method']=='memory_event_submit':return contract.core_receipt(response,request)
        if not isinstance(receipt,dict) or not isinstance(value,dict):return False
        native=(params['kind']=='memory' and receipt.get('storage')=='tencent-l1' and isinstance(receipt.get('checkpoint'),str) and re.fullmatch(r'[a-f0-9]{64}',receipt['checkpoint']))
        if not native and (not isinstance(receipt.get('commit'),str) or not re.fullmatch(r'[a-f0-9]{40}',receipt['commit'])):return False
        remote=receipt.get('state')=='remote-confirmed' and receipt.get('durable') is True and receipt.get('github_confirmed',True) is True and receipt.get('local_durable',True) is True
        local=receipt.get('state')=='local-confirmed' and receipt.get('durable') is False and receipt.get('local_durable') is True and receipt.get('github_confirmed') is False
        if not (remote or local):return False
        try:self._validate_target(value,params['kind'],params['id'])
        except Exception:return False
        if request['method']=='content_set_enabled':
            return value['record'] is not None and value['record']['enabled'] is params['enabled']
        return self._import_same(params['kind'],params.get('record'),value['record'],preserve_enabled=False)

    def _queue_result(self,row,rows=None):
        result=row['response'] if row['state']!='pending' else {'request_id':row['request']['request_id'],'error':{'code':'content_upload_queued','retryable':True},'persistence':{'durable':False}}
        # Derived diagnostics only: never edit a retained request, its CAS base,
        # reconciliation references or ID to make a queued write go through.
        blockers:list[dict[str,str]]=[]
        if blockers or row['state']=='conflict':
            next_action=('Inspect blockers and retained local proposals with content_queue_inspect, then read the current target with content_read. '
                         'Resolve conflicts with an explicitly reviewed content_put using a new stable request_id, the current expected_revision and reconciles.request_ids selecting the exact conflicts. '
                         'A blocked request requires review of its original error and an explicit identical retry after its cause is fixed. '
                         'Fresh CAS writes can proceed independently; reconciliation is optional evidence tracking for reviewed merges. '
                         'Do not delete queue entries, change their CAS bases or use a new ID merely to retry an existing write.')
        elif row['state']=='blocked':
            next_action='Inspect the original request and error with content_queue_inspect, fix its cause, then explicitly retry the exact saved request and request_id. Refresh does not retry blocked writes.'
        elif row['state']=='pending':
            next_action='Server persistence is unconfirmed. Retry the exact saved request and request_id after restoring connectivity; do not create a replacement write or change its CAS base.'
        elif row['state']=='reconciled':
            next_action='This proposal was resolved by a confirmed reconciliation. Keep the original evidence; do not resubmit it.'
        else:next_action='The Server receipt determines persistence; local mapping and running model context are separate.'
        receipt=result.get('persistence',{})
        if row['state']=='confirmed' and row['request']['method']!='memory_event_submit':
            backup=receipt.get('github_backup',{})
            if not isinstance(backup,dict):backup={}
            retry=backup.get('automatic_retry',{})
            if not isinstance(retry,dict):retry={}
            confirmed=receipt.get('state')=='remote-confirmed' and receipt.get('durable') is True
            attention=backup.get('attention_required') is True or backup.get('state')=='review-required' or retry.get('state') in ('paused','review-required')
            if confirmed:
                next_action='Saved on the Server and GitHub backup confirmed. No further save action is needed.'
            elif attention:
                next_action='Saved on the Server. GitHub backup needs attention; inspect the backup diagnostic. Keep the saved request; do not create another write or poll for confirmation.'
            elif retry.get('state')=='waiting':
                next_action='Saved on the Server. GitHub backup is a separate background task. No additional write or polling is needed.'
            else:
                next_action='Saved on the Server. GitHub backup is not yet confirmed. Do not create another write to save the same content.'
            # Only a validated Server receipt can produce this success summary.
            # Keep the original receipt and diagnostics unchanged below it.
            result=dict(write_status={'status':'saved','message':next_action,
                                      'backup_status':'confirmed' if confirmed else 'needs-attention' if attention else 'pending'},**result)
        return dict(result,local_queue={'state':row['state'],'saved':True,'request_sha256':row['request_sha'],
                                       'server_saved':row['state']=='confirmed','github_confirmed':receipt.get('state')=='remote-confirmed' and receipt.get('durable') is True,
                                       'blocked_by':blockers,'resolution_request_id':row['resolution_request_id'],'next_action':next_action})



def normalized_turn(scope, turn, phase, content):
    """Validate normalized turn text and retain the existing deterministic key."""
    if phase not in ('prompt', 'final'):
        raise ValueError('Expected prompt or final phase')
    contract.event_identifier(scope['project_id'])
    contract.event_identifier(scope['session_id'])
    contract.event_identifier(turn)
    contract.text(content, 32768)
    if len(content.encode('utf-16-le')) // 2 > 8192:
        raise ValueError('Native text exceeds bound')
    return hashlib.sha256(contract.encode([scope, turn])).hexdigest()


def submit_turn(adapter, scope, turn, phase, content, *, enabled, lock, clock=None, source_gate=None):
    """Stage a normalized prompt/final pair and submit via the original outbox.

    The host validates native scope and recursion before this call. Stable scope,
    turn and key keep existing request IDs and schema-1 queue bytes compatible.
    """
    from datetime import datetime, timezone
    if not enabled():return {'status':'disabled','data_transferred':False}
    key=normalized_turn(scope,turn,phase,content)
    session=scope['session_id']
    name='hook-turns/'+key+'.json'
    with lock(adapter.cache/'event-hook.lock'):
        if not enabled():return {'status':'disabled','data_transferred':False}
        row=adapter._load(name)
        if phase=='prompt':
            if row is not None:
                if row['scope']!=scope or row['turn_id']!=turn or row['prompt']!=content:raise ValueError('Native turn changed; preserve evidence')
                return {'status':'staged','data_transferred':False}
            directory=adapter.cache/'hook-turns';adapter._safe(directory,adapter.cache)
            if directory.exists() and len(list(directory.iterdir()))>=1024:raise ValueError('Hook staging capacity reached')
            now=clock() if clock else datetime.now(timezone.utc)
            stamp=now.isoformat(timespec='milliseconds').replace('+00:00','Z')
            row={'schema':1,'scope':scope,'turn_id':turn,'prompt':content,'occurred_at':stamp,'final':None}
            adapter._save(name,row)
            return {'status':'staged','data_transferred':False}
        if row is None:return {'status':'skipped-no-prompt','data_transferred':False}
        if set(row)!={'schema','scope','turn_id','prompt','occurred_at','final'} or row['schema']!=1 or row['scope']!=scope or row['turn_id']!=turn or row['final'] not in (None,content):raise ValueError('Native staging changed')
        row['final']=content;adapter._save(name,row)
        event={'contract':'agentbrain.event.v1','event_id':'evt-'+key[:56],
               'project_id':scope['project_id'],'session_id':session,'occurred_at':row['occurred_at'],
               'messages':[{'role':'user','content':row['prompt']},{'role':'assistant','content':content}]}
        request={'request_id':'agentbrain_event_'+key,'method':'memory_event_submit',
                 'params':{'kind':'memory','id':event['event_id'],'event':event}}
        if not enabled():return {'status':'disabled','data_transferred':False}
        # Refresh/write transport can hold content.lock beyond the hook wait.
        # Persist this exact event first under the short append lock. Automatic
        # drains skip it; invoke still owns content.lock before any transfer and
        # rechecks the same immutable request plus the current off gate.
        adapter._enqueue(request)
        return adapter.invoke(request,event_gate=lambda:enabled() and (source_gate is None or source_gate()))
