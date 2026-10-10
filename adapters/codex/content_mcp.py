"""Opt-in signed-device content MCP candidate; stable five-tool bridge unchanged."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parent))
from remote_mcp import Server,ProtocolError,keys,pairs,MAX_MESSAGE
from content_cache import ContentAdapter,contract,validate_policy,ROOT as CLIENT_ROOT
from client_pairing import ComponentClient

class ContentServer(Server):
    def __init__(self,adapter,client_version='0.2.0'):
        super().__init__(None,client_version);self.adapter=adapter

    def invoke(self,name,arguments):
        try:
            if name not in self.adapter.methods() or not isinstance(arguments,dict):raise ValueError()
            required=contract.FIELDS[name]|({'request_id'} if name in contract.WRITES else set())
            if set(arguments)-(contract.FIELDS[name]|{'request_id'}) or required-set(arguments):raise ValueError()
            params=dict(arguments);rid=params.pop('request_id',uuid.uuid4().hex);contract.validate(name,params)
            if not isinstance(rid,str) or not 16<=len(rid)<=128 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in rid):raise ValueError()
        except (ValueError,TypeError):raise ProtocolError(-32602,'Invalid typed content request. Writes require a stable request_id and read revision.') from None
        try:result=self.adapter.invoke({'request_id':rid,'method':name,'params':params})
        except Exception:result={'request_id':rid,'error':{'code':'content_adapter_blocked','retryable':False},'persistence':{'durable':False}}
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
                properties={field:{'type':'string'} for field in fields}
                if 'kinds' in fields:properties['kinds']={'type':'array','items':{'type':'string','enum':kinds},'minItems':1,'uniqueItems':True}
                if 'kind' in fields:properties['kind']['enum']=kinds
                if 'record' in fields:properties['record']={'type':'object','description':'Typed v1 record: see PROTOCOL.md. Rules preserve enabled/trigger; Skills preserve files/dependencies. Server validates exact fields.'}
                required=list(sorted(fields))
                if name in contract.WRITES:properties['request_id']={'type':'string','pattern':'^[A-Za-z0-9_-]{16,128}$'};required.append('request_id')
                tools.append({'name':name,'description':'Approved cloud content only. Server is the sole write boundary; retain CAS revision and request_id on write retries. Local cache refresh does not reload existing model context.','inputSchema':{'type':'object','properties':properties,'required':required,'additionalProperties':False},'annotations':{'readOnlyHint':name not in contract.WRITES,'destructiveHint':name=='content_remove','idempotentHint':True}})
            return {'tools':tools}
        result=super().handle(request)
        if isinstance(request,dict) and request.get('method')=='initialize' and isinstance(result,dict):
            self.adapter.refresh()
            result['serverInfo']['name']='agent-brain-content-codex-candidate'
            result['instructions']='Use only approved cloud Memory/Rule/Skill content through these Server APIs. Read revisions before CAS writes and keep stable request_id on retries; only durable=true confirms GitHub persistence. Native mapping checks cloud changes at startup/tool boundaries. Do not collect native sessions/history, local-only/private material or credentials. File refresh does not replace already loaded context; report scope/dependency/drift blockers. Pairing and publication remain separate authorized operations.'
        return result

def pinned(path,digest):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode&0o077 or path.stat().st_size>65536:raise ValueError()
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
        def methods(self):return contract.methods(client.grant['scopes'])
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
    return {'status':'content-native-protocol-checked','protocol_checked':True,'authentication_checked':True,'tools':sorted(adapter.methods()),'native_changed':False,'model_loaded':False}

def main():
    if sys.argv[1:]==['--self-check']:print(json.dumps(self_check()));return
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('content-policy','content-policy-sha256','device-home','cache'):parser.add_argument('--'+name,required=True)
    for name in ('device-plan','device-plan-sha256','signing-policy','signing-policy-sha256','ssh-profile','ssh-profile-sha256'):parser.add_argument('--'+name)
    parser.add_argument('--codex-home',help='Approved actual Codex home; otherwise honor CODEX_HOME and preserve device-level skills.')
    parser.add_argument('--installation-check',action='store_true',help='Fixed read-only protocol/auth check; does not write cache or native files.')
    parser.add_argument('--component-source-sha256',help='Release policy pin for the separately approved signed component source.')
    parser.add_argument('--client-version',default='0.2.0',help='Verified active version supplied by the pinned startup.')
    args=parser.parse_args()
    if args.component_source_sha256 and hashlib.sha256((CLIENT_ROOT/'native/DeviceComponent.swift').read_bytes()).hexdigest()!=args.component_source_sha256:raise ValueError('Native source review required')
    policy=pinned(args.content_policy,args.content_policy_sha256)
    if args.ssh_profile:
        if any((args.device_plan,args.device_plan_sha256,args.signing_policy,args.signing_policy_sha256,args.component_source_sha256)):raise ValueError('Distinct transport policies required')
        from ssh_content import SSHContentClient
        client=SSHContentClient(args.ssh_profile,args.ssh_profile_sha256)
    else:
        if args.ssh_profile_sha256:raise ValueError('SSH profile required')
        signing=pinned(args.signing_policy,args.signing_policy_sha256)
        client=ComponentClient(args.device_plan,args.device_plan_sha256,signing)
    if args.installation_check:print(json.dumps(installation_check(client,policy,args.client_version)));return
    server=ContentServer(ContentAdapter(args.device_home,args.cache,policy,client,codex_home=args.codex_home),args.client_version)
    while True:
        raw=sys.stdin.buffer.readline(MAX_MESSAGE+1)
        if not raw:break
        rid=None
        try:
            if len(raw)>MAX_MESSAGE:raise ProtocolError(-32600,'Message exceeds 128 KB; connection closed.')
            request=json.loads(raw.decode(),object_pairs_hook=pairs)
            if isinstance(request,dict) and type(request.get('id')) in (str,int):rid=request['id']
            value=server.handle(request)
            if value is None:continue
            response={'jsonrpc':'2.0','id':rid,'result':value}
        except ProtocolError as error:response={'jsonrpc':'2.0','id':rid,'error':{'code':error.code,'message':error.message}}
        except Exception:response={'jsonrpc':'2.0','id':rid,'error':{'code':-32700,'message':'Invalid JSON message.'}}
        print(json.dumps(response,ensure_ascii=False),flush=True)
        if len(raw)>MAX_MESSAGE:break

if __name__=='__main__':
    try:main()
    except Exception:print('Content candidate startup blocked; retain current client and reviewed policies. No credential details returned.',file=sys.stderr);sys.exit(1)
