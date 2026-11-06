"""Opt-in HTTPS device content and native-only durable pairing coordinator.

Profiles contain reviewed metadata only. Codes and tokens remain inside the
dedicated OS credential item, HTTP Authorization and private helper pipes.
"""
import hashlib
import json
from pathlib import Path
import platform
import re
import secrets
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

BASE=Path(__file__).resolve().parent
ROOT=BASE.parent
sys.path.insert(0,str(ROOT/'scripts'))
from brain_http import RemoteClient,NoRedirect,RequestStopped
from client_pairing import EnrollmentTerminal,reviewed_ids,allowed_id
from system_credentials import NativeKeychain,canonical,unique,owned,credential_fields,profile_credentials,credential_paths,store_for_profile
from brain_lock import require_platform,platform_paths,PlatformUnsupported,native_platform


def valid_secret(value):return isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{32,128}',value)
def grant_metadata_check(grant):
    keys={'actor','scopes','artifact_types','ids','adapter','platform'}
    if not isinstance(grant,dict) or set(grant)!=keys or grant['adapter']!='codex'  or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',grant.get('actor','')):raise ValueError('Frozen device grant required')
    if grant['scopes']!=['read','put'] or not isinstance(grant['artifact_types'],list) or not grant['artifact_types'] or len(set(grant['artifact_types']))!=len(grant['artifact_types']) or set(grant['artifact_types'])-{'memory','rule','skill'}:raise ValueError('Reviewed read/put grant required')
    platform_paths(grant['platform'])
    reviewed_ids(grant['artifact_types'],grant['ids'])
    from brain_content import personal_owner
    if not personal_owner(grant['ids']) and (any(not isinstance(v,list) for v in grant['ids'].values()) or sum(map(len,grant['ids'].values()))!=31):raise ValueError('Personal owner or legacy 31 exact approved IDs required')

def grant_check(grant):
    grant_metadata_check(grant)
    require_platform('credential-store', grant['platform'])


def endpoint_check(endpoint):
    if not isinstance(endpoint,str):raise ValueError('HTTPS origin required')
    value=urlsplit(endpoint)
    if value.scheme!='https' or not value.hostname or value.username or value.password or value.query or value.fragment or value.path not in ('','/') or any(c.isspace() or ord(c)<33 or ord(c)==127 for c in endpoint):raise ValueError('Credential-free HTTPS origin required')
    return endpoint.rstrip('/')

def profile_metadata(endpoint, grant, descriptor, state_directory):
    grant_metadata_check(grant)
    target = grant['platform']
    fields=credential_fields(target,descriptor)
    value = {'transport': 'https-system-credential', 'adapter': 'codex',
             'platform': target, 'data_mode': 'personal', 'endpoint': endpoint,
             'grant': grant, 'state_directory': state_directory, **fields}
    validate_profile_metadata(value)
    return value


def validate_profile_metadata(value):
    common = {'schema','transport','adapter','platform','data_mode','endpoint','grant','state_directory'}
    if not isinstance(value,dict) or value.get('transport')!='https-system-credential' or value.get('adapter')!='codex' or value.get('data_mode')!='personal':
        raise ValueError('HTTPS content profile required')
    target = value['platform']; paths = platform_paths(target)
    descriptor=profile_credentials(value)
    if set(value)!=common|set(credential_fields(target,descriptor)):
        raise ValueError('Fixed platform profile metadata required')
    endpoint_check(value['endpoint']);grant_metadata_check(value['grant'])
    if value['grant']['platform']!=target:raise ValueError('Grant binding changed')
    state=paths(value['state_directory'])
    if not state.is_absolute() or '..' in state.parts:raise ValueError('Absolute private state required')
    return value


def profile_file(path,digest):
    require_platform('credential-store')
    path=owned(path)
    if path.stat().st_size>16384 or not re.fullmatch(r'[a-f0-9]{64}',digest or ''):raise ValueError('Pinned private profile required')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('Profile changed')
    value=validate_profile_metadata(json.loads(raw,object_pairs_hook=unique))
    require_platform('credential-store',value['platform'])
    if value['platform']!=native_platform():raise ValueError('Actual credential platform required')
    credential_paths(value)
    return value


def native_store(profile,profile_sha):
    return store_for_profile(profile,profile_sha)


class PairingHTTPS:
    def __init__(self,endpoint):
        self.endpoint=endpoint_check(endpoint)
        self.opener=build_opener(ProxyHandler({}),NoRedirect())

    def call(self,phase,payload,credential=None):
        headers={'Content-Type':'application/json'}
        if credential is not None:
            if not valid_secret(credential):raise ValueError('Invalid device credential')
            headers['Authorization']='Bearer '+credential
        request=Request(self.endpoint+'/v1/pairing/'+phase,data=canonical(payload),headers=headers,method='POST')
        try:
            with self.opener.open(request,timeout=30) as response:
                raw=response.read(16385)
                if response.status!=200:raise ValueError('Pairing unconfirmed')
            if len(raw)>16384:raise ValueError('Pairing reply exceeds limit')
            value=json.loads(raw,object_pairs_hook=unique)
            if not isinstance(value,dict) or 'error' in value:raise ValueError('Pairing unconfirmed')
            return value
        except HTTPError as error:
            # Only an explicit protocol denial is terminal; timeouts, redirects,
            # proxy failures and unknown replies preserve the same recovery.
            if phase in ('prepare','confirm') and error.code==403:
                try:
                    raw=error.read(16385)
                    value=json.loads(raw,object_pairs_hook=unique) if len(raw)<=16384 else {}
                    code=value.get('error',{}).get('code')
                except Exception:code=None
                if phase=='prepare' and code=='pairing_denied':raise EnrollmentTerminal('pairing_prepare_rejected') from None
                if phase=='confirm' and code in ('pairing_expired','device_revoked'):raise EnrollmentTerminal(code) from None
            raise ValueError('Pairing unconfirmed') from None
        except EnrollmentTerminal:raise
        except Exception:raise ValueError('Pairing unconfirmed') from None

    def prepare(self,code,nonce,profile):
        return self.call('prepare',{'code':code,'nonce':nonce,'device':{k:profile[k] for k in ('adapter','platform')}})

    def confirm(self,credential,nonce):return self.call('confirm',{'nonce':nonce},credential)


class DeviceEnrollment:
    def __init__(self,profile,profile_sha,store,transport,secure_input):
        self.profile,self.profile_sha,self.store,self.transport,self.secure_input=profile,profile_sha,store,transport,secure_input

    def check(self,saved):
        basic={'schema','profile_sha256','phase','nonce'}
        if not isinstance(saved,dict) or type(saved.get('schema'))!=int or saved['schema']!=1 or saved.get('profile_sha256')!=self.profile_sha or not valid_secret(saved.get('nonce')):raise ValueError('Enrollment record binding changed')
        if saved.get('phase')=='preparing':
            if set(saved)!=basic|{'code'} or not valid_secret(saved['code']):raise ValueError('Invalid prepare recovery')
        elif saved.get('phase') in ('pending','active'):
            if set(saved)!=basic|{'credential','device_id','grant'} or not valid_secret(saved['credential']) or not re.fullmatch(r'[a-f0-9]{32}',saved.get('device_id','')) or saved['grant']!=self.profile['grant']:raise ValueError('Invalid device recovery')
        else:raise ValueError('Unknown enrollment preserved')
        return saved

    def run(self):
        try:
            with self.store.lease():
                saved=self.store.load()
                if saved is None:
                    code=self.secure_input()
                    if code is None:return {'status':'pairing-cancelled','installed':False}
                    if not valid_secret(code):return {'status':'pairing-invalid-input','installed':False}
                    saved={'schema':1,'profile_sha256':self.profile_sha,'phase':'preparing','code':code,'nonce':secrets.token_urlsafe(24)}
                    # Persist before prepare. Lost replies/restarts reuse code+nonce.
                    self.store.save(saved,None)
                self.check(saved)
                if saved['phase']=='preparing':
                    result=self.transport.prepare(saved['code'],saved['nonce'],self.profile)
                    if set(result)!={'credential','nonce','device_id','grant','phase'} or result['phase']!='pending' or result['nonce']!=saved['nonce'] or result['grant']!=self.profile['grant']:raise ValueError('Unexpected prepare grant')
                    pending={'schema':1,'profile_sha256':self.profile_sha,**result}
                    self.check(pending);self.store.save(pending,saved);saved=pending
                result=self.transport.confirm(saved['credential'],saved['nonce'])
                if result!={'device_id':saved['device_id'],'grant':self.profile['grant'],'phase':'active'}:raise ValueError('Unexpected confirmation')
                active=dict(saved,phase='active')
                if saved!=active:self.store.save(active,saved)
                return {'status':'device-paired','device_id':saved['device_id'],'grant':self.profile['grant'],'installed':False,'next_action':'Apply the separately approved exact owned content installation plan and read-only protocol check.'}
        except EnrollmentTerminal as error:
            return {'status':'pairing-reenrollment-review-required','reason':error.reason,'installed':False}
        except Exception:
            return {'status':'pairing-resume-required','installed':False,'next_action':'Preserve the system credential record and retry the same pinned profile. No raw transport or credential details returned.'}

    def reset_terminal(self,confirm_reset):
        try:
            with self.store.lease():
                saved=self.store.load()
                if saved is None:return {'status':'pairing-not-enrolled','installed':False}
                self.check(saved)
                if saved['phase']=='preparing':
                    try:
                        # Recheck exact code+nonce, never assume a network failure
                        # means rejection. A successful prepare stays recoverable.
                        self.transport.prepare(saved['code'],saved['nonce'],self.profile)
                        return {'status':'pairing-reset-blocked','installed':False}
                    except EnrollmentTerminal as error:
                        if error.reason!='pairing_prepare_rejected':raise ValueError('Unexpected prepare terminal')
                        if not confirm_reset(error.reason,'preparing'):return {'status':'pairing-reset-cancelled','installed':False}
                        self.store.clear(saved)
                        return {'status':'pairing-reenrollment-ready','installed':False}
                try:
                    result=self.transport.confirm(saved['credential'],saved['nonce'])
                    if result!={'device_id':saved['device_id'],'grant':self.profile['grant'],'phase':'active'}:raise ValueError('Unknown confirmation')
                    return {'status':'active-enrollment-preserved','installed':False}
                except EnrollmentTerminal as error:
                    if not confirm_reset(error.reason,saved['device_id']):return {'status':'pairing-reset-cancelled','installed':False}
                    self.store.clear(saved)
                    return {'status':'pairing-reenrollment-ready','installed':False}
        except Exception:return {'status':'pairing-reset-blocked','installed':False}


class PinnedDeviceTask:
    """Explicit one-task consent; no credential access, retry or installation."""
    def __init__(self,path,digest,profile,profile_sha,*,clock=time.time):
        self.path,self.digest,self.profile,self.profile_sha=path,digest,profile,profile_sha
        self.clock=clock
        self.value=self.load()

    def load(self):
        path=owned(self.path)
        raw=path.read_bytes()
        if len(raw)>8192 or hashlib.sha256(raw).hexdigest()!=self.digest:
            raise ValueError('Device task policy drift')
        value=json.loads(raw,object_pairs_hook=unique)
        keys={'schema','enabled','not_before','expires_at','actor','device_id',
              'endpoint','profile_sha256','extract_request','recall_request'}
        if (not isinstance(value,dict) or set(value)!=keys or type(value['schema']) is not int
                or value['schema']!=1 or type(value['enabled']) is not bool
                or type(value['not_before']) is not int or type(value['expires_at']) is not int
                or not 0<value['expires_at']-value['not_before']<=3600
                or value['endpoint']!=self.profile['endpoint'] or value['profile_sha256']!=self.profile_sha
                or value['actor']!=self.profile['grant']['actor']
                or not re.fullmatch(r'[a-f0-9]{32}',value['device_id'])):
            raise ValueError('Finite pinned device task required')
        extract,recall=value['extract_request'],value['recall_request']
        if extract is None:
            from brain_content import validate
            if not isinstance(recall,dict) or set(recall)!={'request_id','method','params'} or recall['method']!='memory_query' or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',recall['request_id']):raise ValueError('Exact read-only task required')
            validate('memory_query',recall['params'])
            if 'selection' not in recall['params']:raise ValueError('Exact read selection required')
            return value
        for request,method in ((extract,'memory_extract'),(recall,'memory_query')):
            if (not isinstance(request,dict) or set(request)!={'request_id','method','params'}
                    or request['method']!=method or not isinstance(request['request_id'],str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',request['request_id'])
                    or not isinstance(request['params'],dict)):
                raise ValueError('One exact task request required')
        e,r=extract['params'],recall['params']
        from brain_content import validate
        validate('memory_query',r)
        if (set(e)!={'project_id','session_id'} or any(not isinstance(x,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',x) for x in e.values())
                or set(r)!={'kind','query','since','until','limit','context'}
                or r['kind']!='memory' or r['since'] is not None or r['until'] is not None
                or type(r['limit']) is not int or not 1<=r['limit']<=2**31-1
                or not r['query'] or 'REPLACE_' in r['query']
                or not isinstance(r['context'],dict) or set(r['context'])!={'project_id','session_id','cross_session'}
                or r['context']['project_id']!=e['project_id'] or r['context']['cross_session'] is not True
                or not isinstance(r['context']['session_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',r['context']['session_id'])
                or r['context']['session_id']==e['session_id'] or extract['request_id']==recall['request_id']):
            raise ValueError('Source and separate recall session required')
        return value

    def check(self,request,device_id):
        value=self.load()
        from brain_content import personal_owner
        if (value!=self.value or value['enabled'] is not True
                or not value['not_before']<=self.clock()<value['expires_at']
                or device_id!=value['device_id'] or not personal_owner(self.profile['grant']['ids'])
                or self.profile['grant']['scopes']!=['read','put']
                or request not in (value['extract_request'],value['recall_request'])):
            raise ValueError('Outside finite owner device task')


class PinnedRecallPolicy:
    """Read-only derived recall for an explicitly approved finite session set."""
    def __init__(self,path,digest,profile,profile_sha,*,clock=time.time):
        self.path,self.digest,self.profile,self.profile_sha,self.clock=path,digest,profile,profile_sha,clock
        self.value=self.load()

    def load(self):
        raw=owned(self.path).read_bytes()
        if len(raw)>16384 or hashlib.sha256(raw).hexdigest()!=self.digest:raise ValueError('Recall policy drift')
        p=json.loads(raw,object_pairs_hook=unique)
        if isinstance(p,dict) and p.get('schema')==2:return self.daily(p)
        fields={'schema','enabled','not_before','expires_at','actor','device_id','endpoint','profile_sha256','project_id','source_sessions','recall_sessions','max_results','off_file'}
        if (not isinstance(p,dict) or set(p)!=fields or type(p['schema']) is not int or p['schema']!=1
                or type(p['enabled']) is not bool or type(p['not_before']) is not int or type(p['expires_at']) is not int
                or not 0<p['expires_at']-p['not_before']<=3600 or p['actor']!=self.profile['grant']['actor']
                or p['endpoint']!=self.profile['endpoint'] or p['profile_sha256']!=self.profile_sha
                or not isinstance(p['device_id'],str) or not re.fullmatch(r'[a-f0-9]{32}',p['device_id'])
                or type(p['max_results']) is not int or not 1<=p['max_results']<=2**31-1):raise ValueError('Finite read-only recall policy required')
        from brain_content import event_identifier
        event_identifier(p['project_id'])
        for name in ('source_sessions','recall_sessions'):
            rows=p[name]
            if not isinstance(rows,list) or not 1<=len(rows)<=16 or any(not isinstance(x,str) for x in rows) or len(set(rows))!=len(rows):raise ValueError('Finite recall sessions required')
            for row in rows:event_identifier(row)
        if set(p['source_sessions'])&set(p['recall_sessions']):raise ValueError('Independent recall sessions required')
        off=Path(p['off_file'])
        if not off.is_absolute() or '..' in off.parts or any(q.is_symlink() for q in (off.parent,*off.parent.parents)):raise ValueError('Canonical recall off marker required')
        return p

    def daily(self,p):
        fields={'schema','enabled','lifecycle','actor','device_id','endpoint','profile_sha256','project_id','source_mode','source_session_id','recall_session_id','source_device_id','max_results','off_file'}
        from brain_content import event_identifier
        if (set(p)!=fields or p['lifecycle']!='until-disabled' or type(p['enabled']) is not bool
                or p['source_mode'] not in ('codex-project','operator-notes') or p['actor']!=self.profile['grant']['actor']
                or p['endpoint']!=self.profile['endpoint'] or p['profile_sha256']!=self.profile_sha
                or not isinstance(p['device_id'],str) or not re.fullmatch(r'[a-f0-9]{32}',p['device_id'])
                or type(p['max_results']) is not int or not 1<=p['max_results']<=2**31-1):raise ValueError('Persistent project recall policy required')
        for k in ('project_id','source_session_id','recall_session_id','source_device_id'):event_identifier(p[k])
        if p['source_session_id']==p['recall_session_id']:raise ValueError('Distinct logical streams required')
        off=Path(p['off_file'])
        if not off.is_absolute() or '..' in off.parts or any(q.is_symlink() for q in (off.parent,*off.parent.parents)):raise ValueError('Canonical source switch required')
        return p

    def check(self,request,device_id):
        p=self.load();off=Path(p['off_file'])
        if p.get('schema')==2:return self.check_daily(request,device_id)
        from brain_content import validate,personal_owner
        if (p!=self.value or not p['enabled'] or not p['not_before']<=self.clock()<p['expires_at']
                or off.exists() or off.is_symlink() or device_id!=p['device_id']
                or self.profile['grant']['scopes']!=['read','put'] or not personal_owner(self.profile['grant']['ids'])
                or not isinstance(request,dict) or set(request)!={'request_id','method','params'} or request['method']!='memory_query'):raise ValueError('Outside scoped recall consent')
        if p.get('schema')==2:
            return self.check_daily(request,device_id)
        q=request['params'];validate('memory_query',q)
        if (set(q)!={'kind','query','since','until','limit','context','source_sessions'}
                or q['source_sessions']!=p['source_sessions'] or q['limit']>p['max_results']
                or q['context']['project_id']!=p['project_id'] or q['context']['session_id'] not in p['recall_sessions']):raise ValueError('Recall scope changed')

    def check_daily(self,request,device_id):
        p=self.load()
        from brain_content import validate,personal_owner
        if (p!=self.value or not p['enabled'] or device_id!=p['device_id']
                or self.profile['grant']['scopes']!=['read','put'] or not personal_owner(self.profile['grant']['ids'])
                or not isinstance(request,dict) or set(request)!={'request_id','method','params'} or request['method']!='memory_query'):
            raise ValueError('Persistent recall grant denied')
        q=request['params'];validate('memory_query',q)
        if (set(q)!={'kind','query','since','until','limit','context','source_scope'} or q['source_scope']!='approved-project'
                or q['limit']>p['max_results'] or q['context']['project_id']!=p['project_id']
                or p['source_mode']=='operator-notes' and q['context']['session_id']!=p['recall_session_id']):
            raise ValueError('Persistent recall scope changed')



class HTTPSContentClient:
    def __init__(self,path,digest,*,store=None,credential_access=True,device_task=None,recall_policy=None):
        self.profile,self.profile_sha=profile_file(path,digest),digest
        self.grant,self.data_mode=self.profile['grant'],'personal'
        self.store=store if store is not None else native_store(self.profile,digest)
        self.enrollment=DeviceEnrollment(self.profile,digest,self.store,None,None)
        self.device_task=PinnedDeviceTask(*device_task,self.profile,digest) if device_task is not None else None
        if device_task is not None and recall_policy is not None:raise ValueError('Distinct task and recall policies required')
        self.recall_policy=PinnedRecallPolicy(*recall_policy,self.profile,digest) if recall_policy is not None else None
        self.device_id=None;self.queue_identity=None
        if credential_access:self.activate_identity()
        self.transport=RemoteClient(self.profile['endpoint'],data_mode='personal',credential_provider=self._credential,write_receipt='local-durable-v1')

    def activate_identity(self):
        saved=self.enrollment.check(self.store.load())
        if saved['phase']!='active' or self.device_id is not None and saved['device_id']!=self.device_id:raise ValueError('Confirmed unchanged device required')
        self.device_id=saved['device_id']
        self.queue_identity={'transport':'https-system-credential','endpoint':self.profile['endpoint'],'profile_sha256':self.profile_sha,'device_id':self.device_id}

    def _credential(self):
        saved=self.enrollment.check(self.store.load())
        if saved['phase']!='active' or self.device_id is None or saved['device_id']!=self.device_id:raise ValueError('Device identity changed; old outbox preserved')
        return saved['credential']

    def recall(self, request):
        """One read through the Server's existing per-device global source gate."""
        from brain_content import global_recall_params, global_recall_result
        if self.device_task is not None or self.recall_policy is not None or request.get('method') not in ('memory_query','memory_global_query'):
            raise ValueError('Normal approved-source recall required')
        if 'read' not in self.grant['scopes'] or 'memory' not in self.grant['artifact_types']:
            raise ValueError('Memory read grant required')
        global_recall_params(request['params'])
        result = self.transport.request(request)
        if 'error' not in result: global_recall_result(result['result'], request['params'], self.device_id)
        return result

    def request(self,request):
        try:
            method=request['method'];params=request['params']
            if method=='memory_query' and self.recall_policy is not None:
                self.recall_policy.check(request,self.device_id)
            elif method=='memory_extract' or method=='memory_query' and (self.device_task is not None or 'context' in params or 'selection' in params):
                if self.device_task is None:raise ValueError('Explicit finite task required')
                self.device_task.check(request,self.device_id)
            elif method not in ('memory_query','memory_event_submit','memory_status','memory_resume','memory_stop','memory_revoke','memory_sync','content_catalog','content_read','content_put','content_set_enabled','client_updates'):raise ValueError('Device-only methods required')
            if method in ('memory_status','memory_resume','memory_stop','memory_revoke','memory_sync'):
                from brain_content import validate
                validate(method,params)
                if 'memory' not in self.grant['artifact_types']:raise ValueError('Memory grant required')
            if method=='content_set_enabled':
                from brain_content import validate, permits_scope
                validate(method,params)
                if not permits_scope(self.grant['scopes'],method):raise ValueError('Content put grant required')
            if method=='memory_query' and 'memory' not in self.grant['artifact_types']:raise ValueError('Memory read grant required')
            if method in ('content_read','content_put','content_set_enabled','memory_event_submit') and not allowed_id(self.grant['ids'],params['kind'],params['id']):raise ValueError('Unreviewed content ID')
            result=self.transport.request(request)
            if method=='memory_query' and (self.device_task is not None or self.recall_policy is not None) and 'error' not in result:
                from brain_content import core_recall
                core_recall(result['result'],params['context'])
                if self.recall_policy is not None:
                    recalled=result['result']['recall']
                    if recalled['L0']!=[]:raise ValueError('Derived-only recall required')
                    count=0
                    for layer in ('L1','L2','L3'):
                        rows=recalled[layer] if layer!='L3' else [recalled[layer]]
                        for row in rows:
                            if row.get('content') is None:continue
                            count+=1
                            proof=row.get('provenance',{});sources=proof.get('sources',[])
                            if (proof.get('status')!='verified-native-generation-graph' or not proof.get('generation_ids') or not sources
                                    or set(proof.get('event_ids',[]))!={s.get('event_id') for s in sources}
                                    or any(s.get('state')!='committed' or (self.recall_policy.value['schema']==1 and s.get('identity',{}).get('session_id') not in self.recall_policy.value['source_sessions'])
                                           or s.get('identity',{}).get('device_id')!=(self.recall_policy.value.get('source_device_id',self.device_id)) or s.get('identity',{}).get('project_id')!=params['context']['project_id'] for s in sources)):
                                raise ValueError('Recall source evidence changed')
                    if count>params['limit']:raise ValueError('Recall response exceeds requested bound')
                if 'selection' in params:
                    from brain_content import exact_capture_response
                    exact_capture_response(result['result']['recall'],params,self.device_task.value['device_id'])
            return result
        except RequestStopped:raise
        except Exception:
            return {'request_id':request.get('request_id') if isinstance(request,dict) else None,'error':{'code':'device_transport_blocked','retryable':False,'stage':'client-policy-or-credential'},'persistence':{'durable':False}}
