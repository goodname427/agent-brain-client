"""Opt-in signed-device content MCP candidate; stable five-tool bridge unchanged."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import queue
import threading
import uuid
from typing import Any
sys.path.insert(0,str(Path(__file__).resolve().parent))
from remote_mcp import Server,ProtocolError,keys,pairs,MAX_MESSAGE
from content_cache import ContentAdapter,contract,validate_policy,ROOT as CLIENT_ROOT
from client_pairing import ComponentClient
from brain_lock import PlatformUnsupported,require_platform,private_mode
from remote_http import RequestControl,RequestStopped
from client_release import UPDATE_GUIDANCE


class ContentParameterError(ProtocolError):
    def __init__(self,message):
        super().__init__(-32602,message)


def worker_commit(client_version):
    """Observe this immutable package's receipt, never the current active pointer."""
    try:
        path=CLIENT_ROOT/'verified.json'
        if path.is_symlink() or not path.is_file() or path.stat().st_size>65536:return None
        receipt=json.loads(path.read_bytes());manifest=receipt['manifest']
        commit=receipt['commit']
        import re
        if manifest['version']!=client_version or not isinstance(commit,str) or not re.fullmatch(r'[a-f0-9]{40}',commit):return None
        if manifest['files']['adapters/codex/content_mcp.py']!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest():return None
        return commit
    except Exception:return None

class ContentServer(Server):
    def __init__(self,adapter,client_version='0.2.0'):
        super().__init__(None,client_version);self.adapter=adapter
        self.client_runtime={'running_worker_version':client_version,'running_worker_commit':worker_commit(client_version)}

    def invoke(self,name,arguments):
        try:
            if name not in self.adapter.methods():raise ContentParameterError('Unknown or unavailable content tool; inspect tools/list.')
            if not isinstance(arguments,dict):raise ContentParameterError('Content tool arguments must be an object.')
            if name=='content_catalog' and set(arguments)&{'query','since','until','limit'}:
                if arguments.get('kinds')!=['memory']:raise ContentParameterError('Catalog query options are a Memory-only retrieval alias. For complete coverage pass kinds only.')
                if 'limit' in arguments and (type(arguments['limit']) is not int or not 1<=arguments['limit']<=3):raise ContentParameterError('content_catalog has no pagination limit. For complete Onboard coverage pass kinds only; Memory query alias limit must be an integer from 1 to 3.')
                if set(arguments)-{'kinds','query','since','until','limit','request_id'}:raise ValueError()
                arguments={key:value for key,value in arguments.items() if key!='kinds'};name='memory_query'
            if name=='memory_query':arguments=dict({'kind':'memory','query':'','since':None,'until':None,'limit':3},**arguments)
            required=contract.FIELDS[name]|({'request_id'} if name in contract.WRITES else set())
            allowed=contract.FIELDS[name]|{'request_id'}|({'reconciles'} if name=='content_put' else set())
            if set(arguments)-allowed or required-set(arguments):raise ValueError()
            params=dict(arguments);reconciles=params.pop('reconciles',None);rid=params.pop('request_id',uuid.uuid4().hex);contract.validate(name,params)
            if not isinstance(rid,str) or not 16<=len(rid)<=128 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in rid):raise ValueError()
        except ContentParameterError:raise
        except (ValueError,TypeError):
            message='Invalid '+name+' arguments; use the exact fields and types in this tool input schema.'
            if name in contract.WRITES:message+=' Writes require a stable request_id and the expected_revision obtained from content_read.'
            raise ContentParameterError(message) from None
        try:
            request={'request_id':rid,'method':name,'params':params}
            result=self.adapter.invoke(request,reconciles=reconciles) if reconciles is not None else self.adapter.invoke(request)
        except RequestStopped:raise
        except Exception:result={'request_id':rid,'error':{'code':'content_adapter_blocked','retryable':False,'stage':'content-validation'},'persistence':{'durable':False}}
        result=dict(result,client_runtime=self.client_runtime)
        return {'isError':'error' in result,'structuredContent':result,'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}]}

    def handle(self,request):
        if isinstance(request,dict) and request.get('method')=='tools/list' and self.state=='ready':
            keys(request,('jsonrpc','id','method','params'),('jsonrpc','id','method'))
            if request['jsonrpc']!='2.0' or type(request['id']) not in (str,int):raise ProtocolError(-32600,'Invalid JSON-RPC request.')
            params=request.get('params',{})
            if isinstance(params,dict) and '_meta' in params:
                if not isinstance(params['_meta'],dict):raise ProtocolError(-32602,'Invalid MCP metadata.')
                params={key:value for key,value in params.items() if key!='_meta'}
            keys(params,('cursor',))
            if params.get('cursor') not in (None,''):raise ProtocolError(-32602,'Unknown cursor.')
            tools=[]
            kinds=getattr(self.adapter,'policy',{}).get('kinds',['memory','rule','skill'])
            for name,fields in contract.FIELDS.items():
                if name not in self.adapter.methods():continue
                properties:dict[str,Any]={field:{'type':'string'} for field in fields}
                if 'kinds' in fields:properties['kinds']={'type':'array','items':{'type':'string','enum':kinds},'minItems':1,'uniqueItems':True}
                if 'kind' in fields:properties['kind']['enum']=kinds
                if 'record' in fields:
                    properties['record']={'oneOf':[
                        {'type':'object','properties':{'title':{'type':'string','maxLength':1024},'description':{'type':'string','maxLength':1024},'body':{'type':'string'},'type':{'type':'string','enum':['user','feedback','reference']}},'required':['title','description','body','type'],'additionalProperties':False},
                        {'type':'object','properties':{'title':{'type':'string','maxLength':1024},'body':{'type':'string'},'enabled':{'type':'boolean'},'trigger':{'type':'object','properties':{'mode':{'type':'string','enum':['always','on-demand','conditional']},'patterns':{'type':'array','items':{'type':'string','maxLength':256},'maxItems':32}},'required':['mode','patterns'],'additionalProperties':False}},'required':['title','body','enabled','trigger'],'additionalProperties':False},
                        {'type':'object','properties':{'description':{'type':'string','maxLength':1024},'enabled':{'type':'boolean'},'files':{'type':'object','properties':{'SKILL.md':{'type':'string'}},'required':['SKILL.md'],'additionalProperties':{'type':'string'},'minProperties':1,'maxProperties':32},'dependencies':{'type':'object','properties':{'commands':{'type':'array','items':{'type':'string'},'maxItems':32},'python_modules':{'type':'array','items':{'type':'string'},'maxItems':32}},'required':['commands','python_modules'],'additionalProperties':False}},'required':['description','enabled','files','dependencies'],'additionalProperties':False}
                    ],'description':'Typed v1 Memory, Rule, or Skill record. Server validates the selected record against kind.'}
                if name=='memory_query':
                    properties.update(query={'type':'string','default':''},since={'type':['string','null'],'default':None,'description':'Inclusive memory update time, ISO8601 with offset; not event occurrence time.'},until={'type':['string','null'],'default':None},limit={'type':'integer','minimum':1,'maximum':3,'default':3})
                    properties['kind']={'type':'string','enum':['memory'],'default':'memory'}
                if name=='content_catalog':
                    properties.update(query={'type':'string','description':'Optional Memory-only retrieval alias; not full inventory. Prefer memory_query for retrieval.'},since={'type':['string','null']},until={'type':['string','null']},limit={'type':'integer','minimum':1,'maximum':3,'description':'Memory retrieval alias only; not pagination. Omit query/since/until/limit for complete AgentBrain target inventory; Harness-native sources are not listed.'})
                required=[] if name=='memory_query' else list(sorted(fields))
                if name in contract.WRITES:properties['request_id']={'type':'string','pattern':'^[A-Za-z0-9_-]{16,128}$'};required.append('request_id')
                if name=='content_put':properties['reconciles']={'type':'object','description':'Explicit local conflict references after merging both versions; never sent to Server. Only durable confirmation resolves them.','properties':{'request_ids':{'type':'array','items':{'type':'string'},'uniqueItems':True,'maxItems':256},'source_keys':{'type':'array','items':{'type':'string'},'uniqueItems':True,'maxItems':256}},'required':['request_ids','source_keys'],'additionalProperties':False}
                description='Approved cloud content only. Server is the sole write boundary; retain CAS revision and request_id on write retries. Transport failures may return a verified stale snapshot with local_cache.stale=true; cache misses never prove remote absence. Local cache refresh does not reload existing model context. '
                if name=='content_catalog':description+='AgentBrain destination records only; this does not discover Harness-native sources. Pass kinds only for the complete authorized target inventory; there is no pagination limit. Query/since/until/limit selects a Memory-only retrieval alias capped at 3. '
                description+='Running worker version: '+self.client_version+'. '+UPDATE_GUIDANCE
                tools.append({'name':name,'description':description,'inputSchema':{'type':'object','properties':properties,'required':required,'additionalProperties':False},'annotations':{'readOnlyHint':name not in contract.WRITES,'destructiveHint':name=='content_remove','idempotentHint':True}})
            return {'tools':tools}
        result=super().handle(request)
        if isinstance(request,dict) and request.get('method')=='initialize' and isinstance(result,dict):
            self.adapter.refresh()
            result['serverInfo']['name']='agent-brain-content-codex-candidate'
            result['instructions']=contract.MEMORY_GUIDANCE+'content_catalog lists authorized AgentBrain destination records only; it does not discover Harness-native sources. '+'Writes require the current expected_revision and stable request_id; retain exact request arguments on retries. '+'Pending/local-confirmed/remote-confirmed and durable fields distinguish client, Server and backup status. '+'Rules/Skills retain enabled state; refreshing mappings does not reload an already running model. '+'Only granted methods and IDs are exposed; pairing and publication remain separate. '+UPDATE_GUIDANCE
        return result

def pinned(path,digest):
    require_platform("private-metadata")
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>65536:raise ValueError()
    private_mode(path)
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError()
    return json.loads(raw,object_pairs_hook=pairs)

def self_check():
    class Adapter:
        def refresh(self):return {'status':'not-wired','native_changed':False}
        def methods(self):return set(contract.FIELDS)
    server=ContentServer(Adapter())
    server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'synthetic-package-check','version':'1'}}})
    server.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
    assert {t['name'] for t in server.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})['tools']}==set(contract.FIELDS)
    assert not any(m in sys.modules for m in ('brain_core','brain_runtime','brain_cli'))
    return {'content_mcp_self_check':'passed','core_imported':False,'native_changed':False,'keychain_accessed':False,'network_used':False}

def installation_check(client,policy,client_version='0.2.0'):
    """Fixed read-only MCP/auth check. No content cache/native mapping is created."""
    validate_policy(policy,client.grant)
    class Adapter:
        def __init__(self):self.policy=policy
        def refresh(self):return {'status':'protocol-only','native_changed':False}
        def methods(self):return contract.methods(client.grant['scopes'])-{'memory_query'}
        def invoke(self,request):
            if request['method']!='content_catalog':raise ValueError('Read-only check')
            return client.request(request)
    adapter=Adapter();server=ContentServer(adapter,client_version)
    first=server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'codex-installation-check','version':'1'}}})
    server.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
    tools=server.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})
    result=server.handle({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'content_catalog','arguments':{'kinds':policy['kinds']}}})
    if first['serverInfo']['name']!='agent-brain-content-codex-candidate' or {t['name'] for t in tools['tools']}!=adapter.methods() or result['isError']:raise ValueError('Content protocol check failed')
    catalog=result['structuredContent']['result']
    import re
    if catalog.get('content_api_version')!=1 or not re.fullmatch(r'[a-f0-9]{40}',catalog.get('commit','')) or not isinstance(catalog.get('artifacts'),list) or len(catalog['artifacts'])>256:raise ValueError('Unconfirmed content catalog')
    for item in catalog['artifacts']:
        contract.validate('content_read',{'kind':item['kind'],'id':item['id']})
        if item['kind'] not in policy['kinds'] or not re.fullmatch(r'[a-f0-9]{64}',item.get('revision','')):raise ValueError('Invalid content catalog')
    return {'status':'content-native-protocol-checked','protocol_checked':True,'authentication_checked':True,'tools':sorted(adapter.methods()),'native_changed':False,'model_loaded':'unknown'}


def serve(server, source, output, *, request_timeout=90, max_pending=256):
    """Keep one adapter executor, but receive cancellation while it is working.

    Read deadlines include queue time. No detached adapter work is left after a
    timeout: its HTTP socket/helper is aborted and its late result is discarded.
    Active writes finish their durable transaction; retries keep the original ID.
    """
    work:queue.Queue=queue.Queue(max_pending)
    pending:dict[str|int,RequestControl]={}
    state_lock=threading.Lock();output_lock=threading.Lock();disconnected=threading.Event()

    def emit(rid, *, result=None, error=None):
        response={'jsonrpc':'2.0','id':rid}
        response['error' if error is not None else 'result']=error if error is not None else result
        with output_lock:
            if disconnected.is_set():return
            try:
                output.write(json.dumps(response,ensure_ascii=False)+'\n');output.flush()
                return
            except (OSError,ValueError):disconnected.set()
        # A closed output cannot acknowledge more work. Keep active durable
        # writes recoverable, but cancel reads and queued operations.
        with state_lock:controls=list(pending.values())
        for control in controls:control.cancel()

    def stopped(rid,error):
        code=-32800 if error.reason=='request_cancelled' else -32001
        emit(rid,error={'code':code,'message':error.reason})

    def execute():
        while True:
            item=work.get()
            if item is None:
                return
            request,rid,control=item
            try:
                if control is not None:control.check()
                params=request.get('params',{}) if isinstance(request,dict) else {}
                read_only=(request.get('method')=='tools/call' and isinstance(params,dict)
                           and params.get('name') in ('content_catalog','content_read','memory_query'))
                if control is not None and read_only:
                    with control.activate():value=server.handle(request)
                    control.close(check=True)
                else:
                    # Never cancel midway through durable enqueue/CAS/rollback.
                    if control is not None:control.close(check=True)
                    value=server.handle(request)
                if value is not None:emit(rid,result=value)
            except RequestStopped as error:stopped(rid,error)
            except ProtocolError as error:
                payload:dict[str,Any]={'code':error.code,'message':error.message}
                if isinstance(error,ContentParameterError):payload['data']={'stage':'parameters','client_runtime':server.client_runtime}
                emit(rid,error=payload)
            except Exception:emit(rid,error={'code':-32700,'message':'Invalid JSON message.'})
            finally:
                if control is not None:
                    control.close()
                    with state_lock:
                        if rid is not None:pending.pop(rid,None)

    worker=threading.Thread(target=execute,name='content-mcp-executor')
    worker.start()
    try:
        while True:
            raw=source.readline(MAX_MESSAGE+1)
            if not raw:break
            if disconnected.is_set():break
            rid=None;control=None
            try:
                if len(raw)>MAX_MESSAGE:raise ProtocolError(-32600,'Message exceeds 128 KB; connection closed.')
                request=json.loads(raw.decode(),object_pairs_hook=pairs)
                if isinstance(request,dict) and type(request.get('id')) in (str,int):rid=request['id']
                if isinstance(request,dict) and request.get('jsonrpc')=='2.0' and request.get('method')=='notifications/cancelled' and 'id' not in request:
                    params=request.get('params',{})
                    if isinstance(params,dict) and type(params.get('requestId')) in (str,int):
                        with state_lock:target=pending.get(params['requestId'])
                        if target is not None:target.cancel()
                    continue
                with state_lock:
                    if rid is not None:
                        if rid in pending:raise ProtocolError(-32600,'Request ID already pending.')
                        if len(pending)>=max_pending:raise ProtocolError(-32000,'MCP request queue full; retry after active work completes.')
                        control=RequestControl(request_timeout);pending[rid]=control
                try:work.put_nowait((request,rid,control))
                except queue.Full:raise ProtocolError(-32000,'MCP request queue full; retry after active work completes.') from None
            except (ProtocolError,ValueError,UnicodeError) as error:
                if control is not None:
                    control.close()
                    with state_lock:
                        if rid is not None:pending.pop(rid,None)
                if isinstance(error,ProtocolError):emit(rid,error={'code':error.code,'message':error.message})
                else:emit(rid,error={'code':-32700,'message':'Invalid JSON message.'})
            if len(raw)>MAX_MESSAGE:break
    finally:
        work.put(None)
        worker.join()

def main():
    if sys.argv[1:]==['--self-check']:print(json.dumps(self_check()));return
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('content-policy','content-policy-sha256','device-home','cache'):parser.add_argument('--'+name,required=True)
    for name in ('device-plan','device-plan-sha256','signing-policy','signing-policy-sha256','ssh-profile','ssh-profile-sha256','https-profile','https-profile-sha256','credential-source-sha256'):parser.add_argument('--'+name)
    parser.add_argument('--codex-home',help='Approved actual Codex home; otherwise honor CODEX_HOME and preserve device-level skills.')
    parser.add_argument('--installation-check',action='store_true',help='Fixed read-only protocol/auth check; does not write cache or native files.')
    parser.add_argument('--import-plan',help='Explicit reviewed private normalized source plan; no native discovery.')
    parser.add_argument('--import-plan-sha256',help='Exact reviewed source-plan digest, required with --import-plan.')
    parser.add_argument('--component-source-sha256',help='Release policy pin for the separately approved signed component source.')
    parser.add_argument('--client-version',default='0.2.0',help='Verified active version supplied by the pinned startup.')
    args=parser.parse_args()
    if args.component_source_sha256 and hashlib.sha256((CLIENT_ROOT/'native/DeviceComponent.swift').read_bytes()).hexdigest()!=args.component_source_sha256:raise ValueError('Native source review required')
    policy=pinned(args.content_policy,args.content_policy_sha256)
    if args.https_profile:
        if any((args.device_plan,args.device_plan_sha256,args.signing_policy,args.signing_policy_sha256,args.component_source_sha256,args.ssh_profile,args.ssh_profile_sha256)):raise ValueError('Distinct transport policies required')
        from https_content import HTTPSContentClient
        client=HTTPSContentClient(args.https_profile,args.https_profile_sha256,credential_access=False)
        source=CLIENT_ROOT/'native/CredentialStore.swift'
        if not args.credential_source_sha256 or hashlib.sha256(source.read_bytes()).hexdigest()!=args.credential_source_sha256 or client.profile['credential_source_sha256']!=args.credential_source_sha256:raise ValueError('Credential helper source review required')
        if Path(client.profile['state_directory'])!=Path(args.device_home)/'.config/agent-brain-client/https':raise ValueError('Credential device binding changed')
        if Path(args.device_home) not in Path(client.profile['credential_helper']).parents:raise ValueError('Device-local helper required')
        client.activate_identity()
    elif args.https_profile_sha256 or args.credential_source_sha256:raise ValueError('HTTPS profile required')
    elif args.ssh_profile:
        if any((args.device_plan,args.device_plan_sha256,args.signing_policy,args.signing_policy_sha256,args.component_source_sha256)):raise ValueError('Distinct transport policies required')
        from ssh_content import SSHContentClient
        client=SSHContentClient(args.ssh_profile,args.ssh_profile_sha256)
    else:
        if args.ssh_profile_sha256:raise ValueError('SSH profile required')
        signing=pinned(args.signing_policy,args.signing_policy_sha256)
        client=ComponentClient(args.device_plan,args.device_plan_sha256,signing)
    if bool(args.import_plan)!=bool(args.import_plan_sha256) or args.installation_check and args.import_plan:raise ValueError('Separate exact import approval required')
    if args.installation_check:print(json.dumps(installation_check(client,policy,args.client_version)));return
    adapter=ContentAdapter(args.device_home,args.cache,policy,client,codex_home=args.codex_home)
    if args.import_plan:
        path=Path(args.import_plan)
        if path.is_symlink() or not path.is_file() or path.stat().st_size>1048576:raise ValueError('Private bounded import plan required')
        private_mode(path)
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=args.import_plan_sha256:raise ValueError('Import plan drift')
        adapter.stage_import(json.loads(raw,object_pairs_hook=pairs))
    serve(ContentServer(adapter,args.client_version),sys.stdin.buffer,sys.stdout)

if __name__=='__main__':
    try:main()
    except PlatformUnsupported as error:print(json.dumps(error.result(served=False,native_changed=False)),file=sys.stderr);sys.exit(2)
    except Exception:print('Content candidate startup blocked; retain current client and reviewed policies. No credential details returned.',file=sys.stderr);sys.exit(1)
