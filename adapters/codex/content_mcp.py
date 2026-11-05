"""Opt-in signed-device content MCP candidate; stable five-tool bridge unchanged."""
import argparse
from collections import deque
import hashlib
import json
import os
import re
import ssl
from urllib.error import HTTPError
from pathlib import Path
import sys
import threading
import uuid
from typing import Any
sys.path.insert(0,str(Path(__file__).resolve().parent))
from remote_mcp import Server,ProtocolError,keys,pairs,MAX_MESSAGE,write_response
from content_cache import CacheBlocked,ContentAdapter,contract,validate_policy,operation_diagnostics,ROOT as CLIENT_ROOT
from client_pairing import ComponentClient
from brain_lock import LockTimeout,PlatformUnsupported,require_platform,private_mode,private_directory,locked
import brain_lock
cache_locked=getattr(brain_lock,'cache_locked',locked)
cache_directory=getattr(brain_lock,'cache_directory',private_directory)
from remote_http import RequestControl,RequestStopped
from client_release import UPDATE_GUIDANCE


RECALL_EVIDENCE_GUIDANCE = (
    'This is the approved content service; synthetic sandbox tools are a separate test service and cannot prove production capture or recall. '
    'Use this currently connected approved-content service for Agent Brain personal memory. '
    'Do not route to agent_brain_synthetic_sandbox based on old memories, similar tool names, test wording or a disabled plugin still appearing in a session. '
    'A missing or failed approved service is a blocker, not permission to enable or fall back to another plugin. '
    'For an isolated offline/replay test, use a temporary Client fixture with a fictional transport; a sandbox health call cannot test the Client outbox. '
    'For a record from the current test or turn, match its source event/session, occurrence time or unique marker; an older matching topic or memory_updated_at alone is not evidence for this run. '
    'If current evidence is missing, report it as unconfirmed; do not substitute an older result. '
    'Hook core-accepted/committed confirms event receipt only. pipeline_state=unverified or model_extraction_verified=false does not confirm model extraction or searchable memory. '
)


class ContentParameterError(ProtocolError):
    def __init__(self,message):
        super().__init__(-32602,message)


TOOL_CONTRACT_VERSION = 3


def current_origin():
    """Directory metadata only; available inside the immutable MCP package."""
    current=Path.cwd().resolve()
    for root in (current,*current.parents):
        marker=root/'.git'
        if marker.is_dir() or marker.is_file():
            repository=root
            if marker.is_file():
                try:
                    if marker.stat().st_size<=4096:
                        line=marker.read_text(encoding='utf-8').strip()
                        if line.startswith('gitdir: '):
                            gitdir=Path(line[8:]);gitdir=(root/gitdir).resolve() if not gitdir.is_absolute() else gitdir.resolve()
                            common=gitdir.parent.parent
                            if gitdir.parent.name=='worktrees' and common.name=='.git':repository=common.parent
                except (OSError,UnicodeError):pass
            return {'kind':'project','cwd':str(current),'project_root':str(root),'repository_root':str(repository)}
    return {'kind':'non-project','cwd':str(current),'project_root':None,'repository_root':None}


def local_tools(kinds, memory_sources, layered_recall=False):
    tools = []
    tools.append({'name':'content_queue_inspect','description':'Inspect this Client\'s retained write queue without sending, retrying or changing requests. With no arguments, list a page of path-free metadata and exact blocking request IDs; use next_cursor for more. With request_id, read the exact original proposal, CAS base, reconciliation references, saved response and cached remote conflict snapshot. The snapshot may be stale: call content_read before an explicit reviewed merge through content_put with a new stable request_id and reconciles.request_ids. Unknown pending outcomes require the identical original request and ID; never replace, rebase or delete them. Terminal conflicts remain reviewable and do not block fresh CAS writes. Uncertain pending outcomes still require exact replay. No device or source discovery.','inputSchema':{'type':'object','properties':{'request_id':{'type':'string','pattern':'^[A-Za-z0-9_-]{16,128}$'},'cursor':{'type':'string','maxLength':96}},'additionalProperties':False},'annotations':{'readOnlyHint':True,'destructiveHint':False,'idempotentHint':True}})
    if layered_recall:
        origin = {'type':'object','properties':{'kind':{'type':'string','enum':['project','non-project']},'cwd':{'type':'string'},'project_root':{'type':['string','null']},'repository_root':{'type':['string','null']}},'required':['kind','cwd','project_root','repository_root'],'additionalProperties':False}
        tools.append({'name':'memory_query','description':'The single memory query and automatic recall entry. Read shared L1/L2/L3 memory through the approved HTTPS Server using the existing device identity. Optional origin defaults to current directory metadata; optional session_id defaults to a distinct reader ID. Server source approval is required; this tool cannot enable capture, enroll a device, import old conversations or call a model. Returns layer-specific committed event provenance and source applicability. Direct and automatically extracted facts share L1 and the same L2/L3 derivation. Empty layers remain unconfirmed. '+RECALL_EVIDENCE_GUIDANCE,'inputSchema':{'type':'object','properties':{'origin':origin,'session_id':{'type':'string','pattern':'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$'},'query':{'type':'string','minLength':1,'maxLength':1024},'limit':{'type':'integer','minimum':1,'default':5,'description':'Native L1 maxResults per namespace; L2/L3 are assembled separately.'},'recall':{'type':'object','properties':{'strategy':{'type':'string','enum':['keyword','embedding','hybrid']},'scoreThreshold':{'type':'number'},'maxCharsPerMemory':{'type':'integer','minimum':0},'maxTotalRecallChars':{'type':'integer','minimum':0},'timeoutMs':{'type':'integer','minimum':1}},'additionalProperties':False,'description':'Native recall settings. Zero character budgets are unlimited; unavailable embeddings use native FTS fallback.'}},'required':['query'],'additionalProperties':False},'annotations':{'readOnlyHint':True,'destructiveHint':False,'idempotentHint':True}})
    tools.append({'name':'content_request_id','description':'Generate a deterministic idempotent write request_id from the completed Codex turn digest, operation and AgentBrain target identity. Different targets/operations in one turn receive different IDs; retries of the same operation get the same ID. Local-only; no transfer.','inputSchema':{'type':'object','properties':{'turn_sha256':{'type':'string','pattern':'^[a-f0-9]{64}$'},'method':{'type':'string','enum':['content_put','content_remove','content_set_enabled']},'kind':{'type':'string','enum':kinds},'id':{'type':'string','pattern':'^[a-z0-9][a-z0-9-]{0,63}$'}},'required':['turn_sha256','method','kind','id'],'additionalProperties':False},'annotations':{'readOnlyHint':True,'destructiveHint':False,'idempotentHint':True}})
    if memory_sources:
        tools.append({'name':'content_sources_inventory','description':'Enumerate only the Codex persistent memories directory. Returns path-free IDs, file fingerprints and per-item processing status in pages of 64; never returns memory text or reads session/transcript files. Inventory is not semantic review or import coverage. Use the returned cursor for the next page; restart if it changes. Local-only read; no Server request or data transfer.','inputSchema':{'type':'object','properties':{'cursor':{'type':'string','maxLength':96}},'additionalProperties':False},'annotations':{'readOnlyHint':True,'destructiveHint':False,'idempotentHint':True}})
        tools.append({'name':'content_sources_read','description':'Read the bounded body of exactly one previously inventoried Codex persistent Memory file, matching its ID and fingerprint. Local-only; never reads transcript/session paths and does not transfer data. Review the returned text for secrets, LocalOnly, customer/employer confidential data and cross-project scope before staging.','inputSchema':{'type':'object','properties':{'source_id':{'type':'string','pattern':'^codex-memory:[a-f0-9]{64}$'},'source_fingerprint':{'type':'string','pattern':'^[a-f0-9]{64}$'}},'required':['source_id','source_fingerprint'],'additionalProperties':False},'annotations':{'readOnlyHint':True,'destructiveHint':False,'idempotentHint':True}})
        tools.append({'name':'content_sources_review','description':'Record an explicit review decision for a previously read exact source. This creates only a path-free status marker in the private cache, never stores text or transfers content. A shareable decision is required before any candidate from this source can be staged. Mark LocalOnly, secrets, or confidential sources excluded; mark unsafe project scope or uncertainty deferred. The legacy no-durable-user-fact reason is not a durability or importance threshold; do not use it to skip ordinary experiences, events, corrections or short-term progress.','inputSchema':{'type':'object','properties':{'source_id':{'type':'string','pattern':'^codex-memory:[a-f0-9]{64}$'},'source_fingerprint':{'type':'string','pattern':'^[a-f0-9]{64}$'},'decision':{'type':'string','enum':['shareable','excluded','deferred','skipped']},'reason_code':{'type':'string','enum':['safe-cross-project-personal-summary','secret-localonly-or-confidential','project-scope-unavailable','uncertain-content','no-durable-user-fact']}},'required':['source_id','source_fingerprint','decision','reason_code'],'additionalProperties':False},'annotations':{'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True}})
        tools.append({'name':'content_sources_stage','description':'Stage the final normalized AgentBrain Memory candidate for one exact source that was read locally and explicitly reviewed as shareable. Cover all authorized shareable sources; do not filter by importance or long-term durability. Include ordinary user experiences, events, decisions, corrections and short-term progress when they can be safely generalized to the shared personal scope. Before staging, call content_read for the target and finish deduplication, merging and corrections while preserving unique source and target information. The same source fingerprint is immutable once staged: repeat only the identical candidate; do not try to revise it in place. This call records a local import-plan entry only; it sends no Server write and is not a save. It returns source_key for local reconciliation. To persist this staged candidate, call content_put with the revision from content_read, a stable request_id and reconciles.source_keys containing that source_key. The reconciliation metadata stays local; content_put sends the record with its CAS revision through the existing outbox. Count it as saved only after the required Server persistence receipt; refresh is not required to perform the write. Never include secrets, LocalOnly, customer/employer confidential data or details unsafe to share across projects.','inputSchema':{'type':'object','properties':{'source_id':{'type':'string','pattern':'^codex-memory:[a-f0-9]{64}$'},'source_fingerprint':{'type':'string','pattern':'^[a-f0-9]{64}$'},'id':{'type':'string','pattern':'^[a-z0-9][a-z0-9-]{0,63}$'},'record':{'type':'object','properties':{'title':{'type':'string','maxLength':1024},'description':{'type':'string','maxLength':1024},'body':{'type':'string'},'type':{'type':'string','enum':['user','feedback','reference']}},'required':['title','description','body','type'],'additionalProperties':False}},'required':['source_id','source_fingerprint','id','record'],'additionalProperties':False},'annotations':{'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True}})
    return tools


LOCAL_TOOLS = frozenset(tool['name'] for tool in local_tools([], True, True) if tool['name']!='memory_query')


class ContentToolUnavailable(ContentParameterError):
    pass


def memory_sources_authorized(policy, grant):
    if policy is None and grant is None:return True  # isolated protocol self-check
    return ('memory' in (policy or {}).get('kinds', []) and
            (grant is None or 'memory' in grant.get('artifact_types', [])))


def tool_names(methods, memory_sources):
    names = set(methods) - contract.HOOK_METHODS
    names |= {tool['name'] for tool in local_tools([], memory_sources, memory_sources and 'memory_query' in methods)}
    if not set(methods) & {'content_put','content_remove','content_set_enabled'}:names.discard('content_queue_inspect')
    return sorted(names)


def enabled_tools(policy, grant):
    """One tool contract for plans, installation checks and the running MCP.

    All names remain bounded by existing read/put/remove and artifact grants.
    Local helpers grant no Server methods, future sources or directory access.
    """
    validate_policy(policy, grant)
    methods = contract.mcp_methods(grant['scopes'])
    if 'memory' not in policy['kinds']:methods -= {'memory_query'}
    if not set(policy['kinds']) & {'rule','skill'}:methods.discard('content_set_enabled')
    return tool_names(methods, memory_sources_authorized(policy, grant))


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
        self.running_worker_commit = worker_commit(client_version)

    @property
    def client_runtime(self):
        """Report this connection; local release inspection never fetches or pairs."""
        value = {
            'running_worker_version': self.client_version,
            'running_worker_commit': self.running_worker_commit,
            'service_mode': 'approved-content',
            'tool_contract_version': TOOL_CONTRACT_VERSION,
            'offered_tools': tool_names(self.adapter.methods(), self._memory_source_tools_authorized()),
            'host_tool_visibility': 'unknown', 'model_loaded': 'unknown',
            'active_package_version': None, 'active_package_commit': None,
            'reload_required': None, 'reload_reason': 'active-package-unobserved',
        }
        if not isinstance(getattr(self.adapter, 'home', None), Path):
            return value
        try:
            from content_install import private_json
            from client_release import ClientReleases
            meta = private_json(self.adapter.home/'.config/agent-brain-client/codex-mcp.json')
            releases = ClientReleases(meta['release_policy'], meta['release_policy_sha256'], inspection=True)
            if Path(releases.policy['runtime']['device_home']).resolve() != self.adapter.home:
                return value
            active = releases.verified_active(serving=True)
            if active is None:
                return value
            value.update(active_package_version=active['version'], active_package_commit=active.get('commit'))
            # A version mismatch proves this worker is old. Equality needs the
            # exact commit too; source checkouts / legacy receipts stay unknown.
            if active['version'] != self.client_version:
                value.update(reload_required=True, reload_reason='running-worker-differs')
            elif self.running_worker_commit is not None and active.get('commit') is not None:
                same = self.running_worker_commit == active['commit']
                value.update(reload_required=not same, reload_reason='running-worker-current' if same else 'running-worker-differs')
            else:
                value['reload_reason'] = 'running-worker-commit-unobserved'
        except Exception:
            pass  # Status failure must not block ordinary memory operations.
        return value

    def _memory_source_tools_authorized(self):
        policy=getattr(self.adapter,'policy',None)
        client=getattr(self.adapter,'client',None)
        grant=getattr(client,'grant',None)
        return memory_sources_authorized(policy, grant)

    def layered_recall(self, arguments):
        if 'memory_query' not in tool_names(self.adapter.methods(), self._memory_source_tools_authorized()):
            raise ContentToolUnavailable('Layered recall requires the existing Memory read grant.')
        try:
            if not isinstance(arguments, dict): raise ValueError()
            arguments=dict(arguments)
            if arguments.pop('kind','memory')!='memory' or arguments.pop('since',None) is not None or arguments.pop('until',None) is not None:raise ValueError()
            arguments.pop('request_id',None)
            if arguments.get('query','')=='':arguments['query']='personal preferences and task progress'
            if 'origin' not in arguments:
                arguments['origin']=current_origin()
            elif isinstance(arguments['origin'],dict) and arguments['origin'].get('kind')=='project' and arguments['origin'].get('repository_root') is None:
                arguments['origin']=dict(arguments['origin'],repository_root=arguments['origin'].get('project_root'))
            arguments.setdefault('session_id','reader-'+uuid.uuid4().hex)
            params = dict({'limit':5,'recall':{}}, **arguments)
            contract.global_recall_params(params)
        except (ValueError, TypeError):
            raise ContentParameterError('Use a text query and optional integer limit. Origin and reader session default automatically.') from None
        except (OSError,ImportError):
            raise ProtocolError(-32010,'Current directory metadata unavailable; supply origin explicitly.') from None
        rid = uuid.uuid4().hex
        try:
            result = self.adapter.recall({'request_id':rid,'method':'memory_query','params':params})
        except RequestStopped: raise
        except Exception:
            result = {'request_id':rid,'error':{'code':'layered_recall_unconfirmed','retryable':False,'stage':'response-validation'},'persistence':{'durable':False}}
        result = dict(result, client_runtime=self.client_runtime)
        return {'isError':'error' in result,'structuredContent':result,'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}]}

    def invoke(self,name,arguments):
        if name in ('memory_query','memory_recall'): return self.layered_recall(arguments)
        try:
            if name=='content_queue_inspect':
                if name not in tool_names(self.adapter.methods(), self._memory_source_tools_authorized()):
                    raise ContentToolUnavailable('Queue inspection requires an existing content write grant.')
                if not isinstance(arguments,dict) or set(arguments)-{'request_id','cursor'} or len(arguments)>1:
                    raise ContentParameterError('Inspect a queue page with optional cursor, or one exact request_id.')
                rid=arguments.get('request_id');cursor=arguments.get('cursor')
                if ('request_id' in arguments and (not isinstance(rid,str) or re.fullmatch(r'[A-Za-z0-9_-]{16,128}',rid) is None) or
                        'cursor' in arguments and (not isinstance(cursor,str) or re.fullmatch(r'[a-f0-9]{64}:[0-9]{1,6}',cursor) is None)):
                    raise ContentParameterError('Use an exact retained request_id or the returned next_cursor.')
                try:result=self.adapter.inspect_queue(request_id=rid,cursor=cursor)
                except LockTimeout:
                    raise ProtocolError(-32000,'Local queue busy; retry inspection after the active operation finishes.') from None
                except OSError:
                    raise ProtocolError(-32010,'Queue inspection unavailable; preserve retained requests and restore private cache access.') from None
                return {'isError':False,'structuredContent':result,'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}]}
            if name.startswith('content_sources_') and not self._memory_source_tools_authorized():
                raise ContentParameterError('Native Memory source tools are outside the granted Memory scope.')
            if name=='content_request_id':
                required={'turn_sha256','method','kind','id'}
                if not isinstance(arguments,dict) or set(arguments)!=required:raise ContentParameterError('Generate an idempotent request ID for one target and operation.')
                if not isinstance(arguments['turn_sha256'],str) or not re.fullmatch(r'[a-f0-9]{64}',arguments['turn_sha256']):raise ContentParameterError('turn_sha256 must be the digest from the active Codex notify event.')
                if arguments['method'] not in ('content_put','content_remove','content_set_enabled'):raise ContentParameterError('Only content write request IDs are supported.')
                contract.validate('content_read',{'kind':arguments['kind'],'id':arguments['id']})
                rid='agentbrain_'+hashlib.sha256(contract.encode([arguments['turn_sha256'],arguments['method'],arguments['kind'],arguments['id']])).hexdigest()
                result={'request_id':rid,'data_transferred':False,'native_changed':False}
                return {'isError':False,'structuredContent':result,'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}]}
            if name=='content_sources_inventory':
                if not isinstance(arguments,dict) or set(arguments)-{'cursor'}:raise ContentParameterError('Inventory accepts only an optional cursor.')
                inventory=self.adapter.inventory_memory_sources();cursor=arguments.get('cursor');offset=0
                if cursor is not None:
                    if not isinstance(cursor,str) or len(cursor)>96 or ':' not in cursor:raise ContentParameterError('Invalid inventory cursor; restart enumeration.')
                    snapshot,raw_offset=cursor.split(':',1)
                    if len(snapshot)!=64 or any(char not in '0123456789abcdef' for char in snapshot) or not raw_offset.isdecimal() or len(raw_offset)>6:raise ContentParameterError('Invalid inventory cursor; restart enumeration.')
                    if snapshot!=inventory['cursor']:raise ContentParameterError('Inventory changed; restart enumeration from the first page.')
                    offset=int(raw_offset)
                sources=inventory['sources'];page=sources[offset:offset+64];next_offset=offset+len(page)
                result={'status':inventory['status'],'counts':inventory['counts'],'cursor':inventory['cursor'],
                        'sources':page,'next_cursor':f"{inventory['cursor']}:{next_offset}" if next_offset<len(sources) else None,
                        'reason':inventory['reason'],'data_transferred':False,'account_complete':False}
                return {'isError':False,'structuredContent':result,'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}]}
            if name=='content_sources_read':
                if not isinstance(arguments,dict) or set(arguments)!={'source_id','source_fingerprint'}:raise ContentParameterError('Read exactly one inventoried source by ID and fingerprint.')
                result=self.adapter.read_memory_source(arguments['source_id'],arguments['source_fingerprint'])
                return {'isError':False,'structuredContent':result,'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}]}
            if name=='content_sources_review':
                if not isinstance(arguments,dict) or set(arguments)!={'source_id','source_fingerprint','decision','reason_code'}:raise ContentParameterError('Review requires one exact source fingerprint, a decision, and a fixed reason code.')
                result=self.adapter.review_memory_source(arguments['source_id'],arguments['source_fingerprint'],arguments['decision'],arguments['reason_code'])
                return {'isError':False,'structuredContent':result,'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}]}
            if name=='content_sources_stage':
                expected={'source_id','source_fingerprint','id','record'}
                if not isinstance(arguments,dict) or set(arguments)!=expected:raise ContentParameterError('Stage requires one locally read source and a normalized AgentBrain Memory record.')
                result=self.adapter.stage_memory_import(arguments['source_id'],arguments['source_fingerprint'],'memory',arguments['id'],arguments['record'])
                return {'isError':False,'structuredContent':result,'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}]}
            if name in contract.HOOK_METHODS or name not in self.adapter.methods():raise ContentToolUnavailable('Tool is not exposed by this worker. Inspect tools/list and the installed tool grant; retry the normal MCP connection if it failed to initialize. Tool absence alone does not prove a remote outage.')
            if not isinstance(arguments,dict):raise ContentParameterError('Content tool arguments must be an object.')
            if name=='content_catalog' and set(arguments)&{'query','since','until','limit'}:
                if arguments.get('kinds')!=['memory']:raise ContentParameterError('Catalog query options are a Memory-only retrieval alias. For complete coverage pass kinds only.')
                if set(arguments)-{'kinds','query','since','until','limit','recall','request_id'}:raise ValueError()
                return self.layered_recall({key:value for key,value in arguments.items() if key!='kinds'})
            required=contract.FIELDS[name]|({'request_id'} if name in contract.WRITES else set())
            allowed=contract.FIELDS[name]|({'cursor'} if name=='content_catalog' else set())|{'request_id'}|({'reconciles','source_context'} if name=='content_put' else {'reconciles'} if name=='content_set_enabled' else set())
            if set(arguments)-allowed or required-set(arguments):raise ValueError()
            params=dict(arguments);reconciles=params.pop('reconciles',None);rid=params.pop('request_id',uuid.uuid4().hex);contract.validate(name,params)
            if not isinstance(rid,str) or not 16<=len(rid)<=128 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in rid):raise ValueError()
        except ContentParameterError:raise
        except CacheBlocked:
            if name=='content_queue_inspect':
                raise ProtocolError(-32010,'Queue inspection unavailable; preserve retained requests and restore the original Client identity, policy and private cache access.') from None
            raise ProtocolError(-32010,'Native Memory source changed or is unavailable; refresh inventory before retrying.') from None
        except (ValueError,TypeError):
            if name=='content_queue_inspect':
                raise ContentParameterError('Invalid queue inspection arguments or queue changed; restart without cursor, then use the returned request_id or next_cursor.') from None
            message='Invalid '+name+' arguments; use the exact fields and types in this tool input schema.'
            if name in contract.WRITES:message+=' Writes require a stable request_id and the expected_revision obtained from content_read.'
            raise ContentParameterError(message) from None
        with operation_diagnostics() as diagnostic:
            try:
                request={'request_id':rid,'method':name,'params':params}
                if name=='content_put' and params['kind']=='memory' and hasattr(self.adapter,'prepare_memory_write'):
                    session=os.environ.get('CODEX_THREAD_ID')
                    if not isinstance(session,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',session):session=None
                    try:origin=current_origin()
                    except OSError:origin=None
                    request=self.adapter.prepare_memory_write(request,{'origin':origin,'session_id':session})
                result=self.adapter.invoke(request,reconciles=reconciles) if reconciles is not None else self.adapter.invoke(request)
            except RequestStopped:raise
            except Exception as error:
                # A busy local lock is transient. Keep the original CAS request
                # and ID for an explicit retry; do not replay anything here.
                busy=isinstance(error,LockTimeout)
                known=(CacheBlocked,LockTimeout,OSError,PermissionError,FileNotFoundError,
                       FileExistsError,NotADirectoryError,IsADirectoryError,TimeoutError,
                       ValueError,TypeError,KeyError,RuntimeError,json.JSONDecodeError,
                       UnicodeDecodeError,UnicodeEncodeError)
                exception_type=type(error).__name__ if type(error) in known else 'Exception'
                result={'request_id':rid,'error':{'code':'content_busy' if busy else 'content_adapter_blocked',
                        'retryable':busy,'stage':diagnostic['stage'],'exception_type':exception_type},
                        'persistence':{'durable':False}}
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
            declared = tool_names(self.adapter.methods(), self._memory_source_tools_authorized())
            for name,fields in contract.FIELDS.items():
                if name not in declared or name=='memory_query':continue
                properties:dict[str,Any]={field:{'type':'string'} for field in fields}
                if name=='content_catalog':properties['cursor']={'type':'string','pattern':'^[a-f0-9]{64}:[0-9]{1,20}$','description':'Use next_cursor from the preceding page; omit to restart.'}
                if 'kinds' in fields:properties['kinds']={'type':'array','items':{'type':'string','enum':kinds},'minItems':1,'uniqueItems':True}
                if 'kind' in fields:properties['kind']['enum']=kinds
                if name=='content_set_enabled':
                    properties['kind']['enum']=[kind for kind in kinds if kind in ('rule','skill')]
                    properties['enabled']={'type':'boolean'}
                if 'record' in fields:
                    properties['record']={'oneOf':[
                        {'type':'object','properties':{'title':{'type':'string','maxLength':1024},'description':{'type':'string','maxLength':1024},'body':{'type':'string'},'type':{'type':'string','enum':['user','feedback','reference']}},'required':['title','description','body','type'],'additionalProperties':False},
                        {'type':'object','properties':{'title':{'type':'string','maxLength':1024},'body':{'type':'string'},'enabled':{'type':'boolean'},'trigger':{'type':'object','properties':{'mode':{'type':'string','enum':['always','on-demand','conditional']},'patterns':{'type':'array','items':{'type':'string','maxLength':256},'maxItems':32}},'required':['mode','patterns'],'additionalProperties':False,'allOf':[{'if':{'properties':{'mode':{'const':'conditional'}},'required':['mode']},'then':{'properties':{'patterns':{'minItems':1}}},'else':{'properties':{'patterns':{'maxItems':0}}}}]}},'required':['title','body','enabled','trigger'],'additionalProperties':False},
                        {'type':'object','properties':{'description':{'type':'string','maxLength':1024},'enabled':{'type':'boolean'},'files':{'type':'object','properties':{'SKILL.md':{'type':'string'}},'required':['SKILL.md'],'additionalProperties':{'type':'string'},'minProperties':1,'maxProperties':32},'dependencies':{'type':'object','properties':{'commands':{'type':'array','items':{'type':'string','pattern':'^[a-zA-Z0-9][a-zA-Z0-9_.+-]{0,63}(?![\\s\\S])'},'uniqueItems':True,'maxItems':32},'python_modules':{'type':'array','items':{'type':'string','pattern':'^[a-zA-Z0-9][a-zA-Z0-9_.+-]{0,63}(?![\\s\\S])'},'uniqueItems':True,'maxItems':32}},'required':['commands','python_modules'],'additionalProperties':False}},'required':['description','enabled','files','dependencies'],'additionalProperties':False}
                    ],'description':'Typed v1 Memory, Rule, or Skill record. Server validates the selected record against kind.'}
                required=[] if name=='memory_query' else list(sorted(fields))
                if name in contract.WRITES:properties['request_id']={'type':'string','pattern':'^[A-Za-z0-9_-]{16,128}$'};required.append('request_id')
                if name in ('content_put','content_set_enabled'):properties['reconciles']={'type':'object','description':'Local-only references to staged source plans or write conflicts; never sent to Server. Inspect retained proposals with content_queue_inspect and read the current target before merging. request_ids select only retained conflicts; recover pending outcomes by identical retry first. A still-inventoried source plan can be reconciled only by the exact staged record; a conflicted source plan requires an explicit merge of source and remote versions. Server persistence confirmation resolves selected conflicts.','properties':{'request_ids':{'type':'array','items':{'type':'string'},'uniqueItems':True,'maxItems':256},'source_keys':{'type':'array','items':{'type':'string'},'uniqueItems':True,'maxItems':256}},'required':['request_ids','source_keys'],'additionalProperties':False}
                if name=='content_set_enabled':
                    properties['reconciles']['properties']['source_keys']['maxItems']=0
                    properties['reconciles']['description']='Optional explicit reconciliation of retained activation conflicts on this same target. Read the current target first; use a new request_id and current CAS revision. Source imports and ordinary edit conflicts cannot be resolved by activation.'
                if name=='content_put':properties['source_context']={'type':'object','description':'Optional current Memory writing origin and actual host session_id. Directory metadata defaults locally; unavailable session is null, never guessed from the Server. Preserved unchanged on retries.','properties':{'origin':{'type':['object','null']},'session_id':{'type':['string','null']}},'required':['origin','session_id'],'additionalProperties':False}
                description='Approved cloud content only. Server is the sole write boundary; retain CAS revision and request_id on write retries. Transport failures may return a verified stale snapshot with local_cache.stale=true; cache misses never prove remote absence. Local cache refresh does not reload existing model context. '
                if name=='content_set_enabled':description+='Only on an explicit user enable/disable request for this exact Rule/Skill. Changes enabled alone on an existing record; ordinary content_put edits must preserve enabled. Check local_refresh separately from persistence; a saved change may still require a local mapping retry. '
                if name=='content_put':description+='Preserve existing Rule/Skill enabled state; create disabled. Use content_set_enabled only for explicit user activation requests. '
                if name=='content_catalog':description+='AgentBrain destination records only; this does not discover Harness-native sources. Omit cursor for the first authorized inventory page, then follow next_cursor until absent. Use memory_query for retrieval. '
                if name in ('memory_query','content_read','content_catalog'):description+=RECALL_EVIDENCE_GUIDANCE
                description+='Running worker version: '+self.client_version+'. '+UPDATE_GUIDANCE
                tools.append({'name':name,'description':description,'inputSchema':{'type':'object','properties':properties,'required':required,'additionalProperties':False},'annotations':{'readOnlyHint':name not in contract.WRITES,'destructiveHint':name=='content_remove','idempotentHint':True}})
            tools.extend(tool for tool in local_tools(kinds, self._memory_source_tools_authorized(), 'memory_query' in declared) if tool['name'] in declared)
            return {'tools':tools}
        result=super().handle(request)
        if isinstance(request,dict) and request.get('method')=='initialize' and isinstance(result,dict):
            mapping=self.adapter.refresh()
            result['content_mapping']=mapping
            result['serverInfo']['name']='agent-brain-content-codex-candidate'
            result['instructions']=RECALL_EVIDENCE_GUIDANCE+contract.MEMORY_GUIDANCE+'content_catalog lists authorized AgentBrain destination records only; it does not discover Harness-native sources. '+'Writes require the current expected_revision and stable request_id; retain exact request arguments on retries. '+'Pending/local-confirmed/remote-confirmed and durable fields distinguish client, Server and backup status. '+'Ordinary edits retain Rule/Skill enabled state; content_set_enabled requires an explicit user activation request. Refreshing mappings does not reload an already running model. '+'Only granted methods and IDs are exposed; pairing and publication remain separate. '+'Native mapping result: '+json.dumps(mapping,sort_keys=True)+'. '+UPDATE_GUIDANCE
        return result

def pinned(path,digest):
    require_platform("private-metadata")
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>65536:raise ValueError()
    private_mode(path)
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError()
    return json.loads(raw,object_pairs_hook=pairs)

def validate_hook_policy(value,*,profile_sha256,content_policy_sha256):
    """Private exact pin, finite sessions; this does not enroll native threads."""
    fields={'schema','enabled','profile_sha256','content_policy_sha256','off_file','scopes'}
    daily=isinstance(value,dict) and value.get('schema')==2
    if daily:
        if set(value)!=fields|{'lifecycle','source_mode','notes_directory'} or value.get('lifecycle')!='until-disabled' or value.get('source_mode') not in ('codex-project','operator-notes'):raise ValueError('Persistent source grant required')
        shape=dict(value,schema=1);shape.pop('lifecycle');shape.pop('source_mode');shape.pop('notes_directory')
    else:shape=value
    if not isinstance(value,dict) or set(shape) not in (fields,fields|{'not_before','expires_at'}) or shape['schema']!=1 or type(value['enabled']) is not bool:raise ValueError('Exact hook policy required')
    if 'not_before' in value and (type(value['not_before']) is not int or type(value['expires_at']) is not int or not 0<value['expires_at']-value['not_before']<=3600):raise ValueError('Finite source window required')
    if value['profile_sha256']!=profile_sha256 or value['content_policy_sha256']!=content_policy_sha256:raise ValueError('Hook destination policy changed')
    off=Path(value['off_file'])
    if not off.is_absolute() or '..' in off.parts or off.is_symlink() or off.parent.resolve()!=off.parent:raise ValueError('Private absolute off switch required')
    private_mode(off.parent,directory=True,owner=True)
    scopes=value['scopes']
    if not isinstance(scopes,list) or len(scopes)>16 or value['enabled'] and not scopes:raise ValueError('Finite hook scopes required')
    seen=set()
    for scope in scopes:
        if not isinstance(scope,dict) or set(scope)!={'cwd','project_id','session_id'}:raise ValueError('Exact hook scope required')
        contract.event_identifier(scope['project_id'])
        if not (daily and value['source_mode']=='codex-project' and scope['session_id'] is None):contract.event_identifier(scope['session_id'])
        path=Path(scope['cwd'])
        if not path.is_absolute() or path.is_symlink() or str(path.resolve())!=scope['cwd'] or scope['session_id'] in seen:raise ValueError('Canonical unique project/session required')
        seen.add(scope['session_id'])
    return value


def hook_enabled(policy):
    # Existence includes symlinks: every off marker fails closed, before stdin.
    off=Path(policy['off_file'])
    import time
    window='not_before' not in policy or policy['not_before']<=time.time()<policy['expires_at']
    return policy['enabled'] is True and window and not off.exists() and not off.is_symlink()


def native_hook_fields(policy,payload):
    """Validate text and finite metadata before credential activation."""
    if not isinstance(payload,dict) or payload.get('hook_event_name') not in ('UserPromptSubmit','Stop'):raise ValueError('Unsupported native hook')
    session=payload.get('session_id');turn=payload.get('turn_id')
    contract.event_identifier(session);contract.event_identifier(turn)
    scope=next((scope for scope in policy['scopes'] if (scope['session_id']==session or policy.get('lifecycle')=='until-disabled' and policy.get('source_mode')=='codex-project' and scope['session_id'] is None) and payload.get('cwd')==scope['cwd']),None)
    if scope is None:raise ValueError('Unregistered native scope')
    scope=dict(scope,session_id=session)
    kind=payload['hook_event_name']
    if kind=='Stop' and payload.get('stop_hook_active') is not False:raise ValueError('Recursive or unspecified Stop rejected')
    content=payload.get('prompt' if kind=='UserPromptSubmit' else 'last_assistant_message')
    from brain_memory import normalized_turn
    key=normalized_turn(scope,turn,'prompt' if kind=='UserPromptSubmit' else 'final',content)
    return scope,turn,kind,content,key


def run_event_hook(adapter,policy,payload,*,clock=None,source_gate=None):
    """One native hook through the official Client outbox; no history reads.

    The caller validates the pinned policy before reading stdin and activating
    the credential store. Tests inject only the ordinary public Client classes.
    """
    if not hook_enabled(policy):return {'status':'disabled','data_transferred':False}
    if policy.get('schema')==2 and not callable(source_gate):raise ValueError('Persistent capture requires the managed source entry')
    if getattr(adapter.client,'profile_sha',policy['profile_sha256'])!=policy['profile_sha256']:raise ValueError('Hook Client destination changed')
    scope,turn,kind,content,key=native_hook_fields(policy,payload)
    from brain_memory import submit_turn
    return submit_turn(adapter,scope,turn,'prompt' if kind=='UserPromptSubmit' else 'final',content,enabled=lambda:hook_enabled(policy),
                       lock=cache_locked,clock=clock,source_gate=source_gate)

def self_check():
    class Adapter:
        def refresh(self):return {'status':'not-wired','native_changed':False}
        def methods(self):return set(contract.FIELDS)
    server=ContentServer(Adapter())
    server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'synthetic-package-check','version':'1'}}})
    server.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
    names=[t['name'] for t in server.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})['tools']]
    assert len(names)==len(set(names))
    assert set(names)==set(tool_names(server.adapter.methods(), server._memory_source_tools_authorized()))
    assert not any(m in sys.modules for m in ('brain_core','brain_runtime','brain_cli'))
    return {'content_mcp_self_check':'passed','core_imported':False,'native_changed':False,'keychain_accessed':False,'network_used':False}

STARTUP_STAGES={
    'arguments','component-source-check','content-policy-validation',
    'https-profile-load','credential-source-validation','device-binding-validation',
    'identity-activation','ssh-profile-load','signing-policy-validation',
    'device-component-load','transport-policy-validation','installation-check',
    'content-adapter-start','import-plan-validation','mcp-serve',
    'event-hook-policy-validation','event-hook-input-read',
    'event-hook-payload-validation','event-hook-run'
}
def emit_startup_failure(stage,error,stream=None):
    """Emit a bounded diagnostic; exception values and tracebacks are never serialized."""
    if stream is None:stream=sys.stderr
    if isinstance(error,ssl.SSLError):
        error_class='SSLError';error_code='startup_transport_error'
    elif isinstance(error,HTTPError):
        error_class='HTTPError';error_code='startup_transport_error'
    elif isinstance(error,OSError):
        error_class='OSError';error_code='startup_io_error'
    elif isinstance(error,ValueError):
        error_class='ValueError';error_code='startup_validation_error'
    elif isinstance(error,KeyError):
        error_class='KeyError';error_code='startup_validation_error'
    elif isinstance(error,TypeError):
        error_class='TypeError';error_code='startup_validation_error'
    elif isinstance(error,RuntimeError):
        error_class='RuntimeError';error_code='startup_blocked'
    elif type(error).__name__ in ('TimeoutExpired','SSLError','HTTPError'):
        error_class=type(error).__name__;error_code='startup_transport_error'
    else:
        error_class='StartupError';error_code='startup_blocked'
    safe_stage=stage if stage in STARTUP_STAGES else 'startup'
    print(json.dumps({'stage':safe_stage,'error_class':error_class,'error_code':error_code}),file=stream)
    return 1


class SafeArgumentParser(argparse.ArgumentParser):
    """Keep invalid argument values out of stderr."""
    def error(self,message):
        sys.exit(emit_startup_failure('arguments',ValueError('invalid_arguments')))


# Codex invokes notify once after a turn and passes the event as one JSON argv.
# An internal marker makes the queued capture turn a one-shot, not a loop.
_NOTIFY_CAPTURE_MARKER='AGENTBRAIN_NOTIFY_CAPTURE:'
_NOTIFY_CAPTURE_INSTRUCTIONS=(
    'Review the immediately preceding completed Codex turn for user-stated preferences, '
    'facts, experiences, events, corrections, or short-term progress. Do not filter '
    'these out solely because they are ordinary, temporary, or not important. Use only '
    'direct user statements as evidence; ignore assistant '
    'guesses and tool output. Process only the supported, authorized Codex Memories '
    'scope exposed by content_sources_inventory/read/review/stage. Read every '
    'eligible source to completion in bounded pages; do not claim account-wide or '
    'session-history coverage. Do not count unread items as complete. Generalize '
    'ordinary user project experience safely for the shared personal Memory scope; '
    'exclude secrets, LocalOnly, confidential or customer-identifying content, and '
    'report unsafe scope as deferred. For memory writes use content_read/CAS and '
    'content_request_id with the supplied turn digest. Honor explicit forgetting '
    'with the existing authorized content_remove path. Do not read transcripts or '
    'store raw conversation text. Never claim durable save without the Server receipt.'
)


def codex_notify_capture(event):
    """Validate the documented notify payload and prepare one capture message."""
    if not isinstance(event,dict) or event.get('type')!='agent-turn-complete':return None
    thread_id=event.get('thread-id');turn_id=event.get('turn-id');messages=event.get('input-messages')
    if (not isinstance(thread_id,str) or not thread_id or len(thread_id)>128 or
            not isinstance(turn_id,str) or not turn_id or len(turn_id)>256 or
            not isinstance(messages,list) or not messages or len(messages)>128):return None
    if any(not isinstance(item,str) or len(item)>65536 for item in messages):return None
    if any(item.startswith(_NOTIFY_CAPTURE_MARKER) for item in messages):return None
    last=event.get('last-assistant-message','')
    if not isinstance(last,str) or len(last)>65536:return None
    turn_digest=hashlib.sha256(turn_id.encode('utf-8')).hexdigest()
    message=(_NOTIFY_CAPTURE_MARKER+turn_id+'\nTurn digest: '+turn_digest+'\n'+_NOTIFY_CAPTURE_INSTRUCTIONS)
    return {'thread_id':thread_id,'turn_id':turn_id,'turn_sha256':turn_digest,
            'message':message,'input_message_count':len(messages)}


def codex_notify_main(argv=None,runner=None):
    """Keep the legacy notify CLI flag as a safe no-op while capture is deferred."""
    return 0


def installation_check(client,policy,client_version='0.2.0'):
    """Fixed read-only MCP/auth check. No content cache/native mapping is created."""
    validate_policy(policy,client.grant)
    class Adapter:
        def __init__(self):self.policy=policy
        def refresh(self):return {'status':'protocol-only','native_changed':False}
        def methods(self):return set(enabled_tools(policy, client.grant))-LOCAL_TOOLS
        def invoke(self,request):
            if request['method']!='content_catalog':raise ValueError('Read-only check')
            return client.request(request)
    adapter=Adapter();server=ContentServer(adapter,client_version)
    first=server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'codex-installation-check','version':'1'}}})
    server.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
    tools=server.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})
    result=server.handle({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'content_catalog','arguments':{'kinds':policy['kinds']}}})
    tool_names=[tool['name'] for tool in tools['tools']]
    expected_tools=set(enabled_tools(policy, client.grant))
    if first['serverInfo']['name']!='agent-brain-content-codex-candidate' or len(tool_names)!=len(set(tool_names)) or set(tool_names)!=expected_tools or result['isError']:raise ValueError('Content protocol check failed')
    catalog=result['structuredContent']['result']
    import re
    contract.validate_catalog(catalog)
    for item in catalog['artifacts']:
        contract.validate('content_read',{'kind':item['kind'],'id':item['id']})
        if item['kind'] not in policy['kinds'] or not re.fullmatch(r'[a-f0-9]{64}',item.get('revision','')):raise ValueError('Invalid content catalog')
    return {'status':'content-native-protocol-checked','protocol_checked':True,'authentication_checked':True,'tool_contract_version':TOOL_CONTRACT_VERSION,'tools':sorted(expected_tools),'native_changed':False,'model_loaded':'unknown'}


def serve(server, source, output, *, request_timeout=90, max_pending=256):
    """Keep one adapter executor, but receive cancellation while it is working.

    Read deadlines include queue time. No detached adapter work is left after a
    timeout: its HTTP socket/helper is aborted and its late result is discarded.
    Active writes finish their durable transaction; retries keep the original ID.
    """
    work:deque=deque()
    pending:dict[str|int,RequestControl]={}
    state_lock=threading.Lock();available=threading.Condition(state_lock)
    output_lock=threading.Lock();disconnected=threading.Event();receiving=True

    def emit(rid, *, result=None, error=None):
        response={'jsonrpc':'2.0','id':rid}
        response['error' if error is not None else 'result']=error if error is not None else result
        with output_lock:
            if disconnected.is_set():return
            try:
                write_response(output,response)
                return
            except (OSError,ValueError):disconnected.set()
        # A closed output cannot acknowledge more work. Keep active durable
        # writes recoverable, but cancel reads and queued operations.
        with state_lock:controls=list(pending.values())
        for control in controls:control.cancel()

    def stopped(rid,error,stage='execution'):
        code=-32800 if error.reason=='request_cancelled' else -32001
        emit(rid,error={'code':code,'message':error.reason,'data':{'stage':stage}})

    def stop_queued(item):
        # Cancellation must acknowledge and remove queued work even while a
        # durable write owns the executor. Never detach active adapter work.
        _,rid,control=item
        with available:
            if item not in work:return
            work.remove(item)
            if pending.get(rid) is control:pending.pop(rid)
        control.close()
        stopped(rid,RequestStopped(control.reason),'queue')

    def execute():
        while True:
            with available:
                while not work and receiving:available.wait()
                if not work:return
                item=work.popleft()
            request,rid,control=item
            try:
                if disconnected.is_set():
                    if control is not None:control.cancel()
                    continue
                if control is not None:control.check()
                params=request.get('params',{}) if isinstance(request,dict) else {}
                read_only=(request.get('method')=='tools/call' and isinstance(params,dict)
                           and params.get('name') in ('content_catalog','content_read','memory_query','memory_recall'))
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
                if isinstance(error,ContentToolUnavailable):payload['data']={'stage':'tools-ready','error_code':'tool-not-exposed','client_runtime':server.client_runtime}
                elif isinstance(error,ContentParameterError):payload['data']={'stage':'parameters','client_runtime':server.client_runtime}
                elif error.code==-32002:payload['data']={'stage':'handshake','error_code':'mcp-not-ready'}
                emit(rid,error=payload)
            except Exception:emit(rid,error={'code':-32603,'message':'MCP operation failed; request JSON was valid.'})
            finally:
                if control is not None:
                    control.close()
                    with state_lock:
                        if rid is not None and pending.get(rid) is control:pending.pop(rid)

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
                with available:
                    if disconnected.is_set():break
                    if rid is not None:
                        if rid in pending:raise ProtocolError(-32600,'Request ID already pending.')
                        if len(pending)>=max_pending:raise ProtocolError(-32000,'MCP request queue full; retry after active work completes.')
                        control=RequestControl(request_timeout);pending[rid]=control
                    if len(work)>=max_pending:raise ProtocolError(-32000,'MCP request queue full; retry after active work completes.')
                    item=(request,rid,control)
                    work.append(item);available.notify()
                if control is not None:
                    try:control.register(lambda item=item:stop_queued(item))
                    except RequestStopped:pass  # The queued callback or executor replied.
            except (ProtocolError,ValueError,UnicodeError) as error:
                if control is not None:
                    control.close()
                    with state_lock:
                        if rid is not None:pending.pop(rid,None)
                if isinstance(error,ProtocolError):emit(rid,error={'code':error.code,'message':error.message})
                else:emit(rid,error={'code':-32700,'message':'Invalid JSON message.'})
            if len(raw)>MAX_MESSAGE:break
    finally:
        with available:
            receiving=False;available.notify()
        worker.join()

_STARTUP_STAGE='arguments'

def main():
    global _STARTUP_STAGE
    _STARTUP_STAGE='arguments'
    if sys.argv[1:2]==['--event-hook-off']:
        if len(sys.argv)!=3:raise ValueError('Exact off marker required')
        target=Path(sys.argv[2])
        if not target.is_absolute() or target.is_symlink() or target.parent.resolve()!=target.parent or not target.parent.is_dir():raise ValueError('Existing private off directory required')
        private_mode(target.parent,directory=True,owner=True)
        from codex_connect import atomic
        atomic(target,b'capture disabled\n',0o600)
        print('{}');return
    if sys.argv[1:2]==['--codex-notify']:
        raise SystemExit(codex_notify_main())
    if sys.argv[1:]==['--self-check']:print(json.dumps(self_check()));return
    parser=SafeArgumentParser(description=__doc__)
    for name in ('content-policy','content-policy-sha256','device-home','cache'):parser.add_argument('--'+name,required=True)
    for name in ('device-plan','device-plan-sha256','signing-policy','signing-policy-sha256','ssh-profile','ssh-profile-sha256','https-profile','https-profile-sha256','credential-source-sha256'):parser.add_argument('--'+name)
    parser.add_argument('--codex-home',help='Approved actual Codex home; otherwise honor CODEX_HOME and preserve device-level skills.')
    parser.add_argument('--installation-check',action='store_true',help='Fixed read-only protocol/auth check; does not write cache or native files.')
    parser.add_argument('--mapping-refresh',action='store_true',help='Finite native mapping after installation; no source discovery or collection.')
    parser.add_argument('--event-hook',action='store_true',help='One finite authorized native event; no refresh, inventory or installer.')
    parser.add_argument('--event-hook-policy');parser.add_argument('--event-hook-policy-sha256')
    parser.add_argument('--import-plan',help='Explicit reviewed private normalized source plan; no native discovery.')
    parser.add_argument('--import-plan-sha256',help='Exact reviewed source-plan digest, required with --import-plan.')
    parser.add_argument('--component-source-sha256',help='Release policy pin for the separately approved signed component source.')
    parser.add_argument('--client-version',default='0.2.0',help='Verified active version supplied by the pinned startup.')
    args=parser.parse_args()
    _STARTUP_STAGE='component-source-check'
    if args.component_source_sha256 and hashlib.sha256((CLIENT_ROOT/'native/DeviceComponent.swift').read_bytes()).hexdigest()!=args.component_source_sha256:raise ValueError('Native source review required')
    if args.mapping_refresh and (args.installation_check or args.event_hook or args.import_plan):raise ValueError('Distinct mapping mode required')
    _STARTUP_STAGE='content-policy-validation'
    policy=pinned(args.content_policy,args.content_policy_sha256)
    hook_policy=None;hook_payload=None
    if args.event_hook:
        _STARTUP_STAGE='event-hook-policy-validation'
        if args.installation_check or args.import_plan or not args.https_profile:raise ValueError('Distinct HTTPS native hook invocation required')
        hook_policy=validate_hook_policy(pinned(args.event_hook_policy,args.event_hook_policy_sha256),profile_sha256=args.https_profile_sha256,content_policy_sha256=args.content_policy_sha256)
        if not hook_enabled(hook_policy):print('{}');return
        _STARTUP_STAGE='event-hook-input-read'
        raw=sys.stdin.buffer.read(1048577)
        if len(raw)>1048576:raise ValueError('Hook input exceeds bound')
        _STARTUP_STAGE='event-hook-payload-validation'
        hook_payload=json.loads(raw,object_pairs_hook=pairs)
        native_hook_fields(hook_policy,hook_payload)
    elif args.event_hook_policy or args.event_hook_policy_sha256:raise ValueError('Explicit hook mode required')
    if args.https_profile:
        _STARTUP_STAGE='https-profile-load'
        if any((args.device_plan,args.device_plan_sha256,args.signing_policy,args.signing_policy_sha256,args.component_source_sha256,args.ssh_profile,args.ssh_profile_sha256)):raise ValueError('Distinct transport policies required')
        from https_content import HTTPSContentClient
        client=HTTPSContentClient(args.https_profile,args.https_profile_sha256,credential_access=False)
        _STARTUP_STAGE='credential-source-validation'
        source=CLIENT_ROOT/('scripts/brain_windows.py' if client.profile['platform'].startswith('win32-') else 'native/CredentialStore.swift')
        if not args.credential_source_sha256 or hashlib.sha256(source.read_bytes()).hexdigest()!=args.credential_source_sha256 or (client.profile.get('credential_source_sha256') or client.profile['credential_backend']['source_sha256'])!=args.credential_source_sha256:raise ValueError('Credential helper source review required')
        _STARTUP_STAGE='device-binding-validation'
        if Path(client.profile['state_directory'])!=Path(args.device_home)/'.config/agent-brain-client/https':raise ValueError('Credential device binding changed')
        if 'credential_helper' in client.profile and Path(args.device_home) not in Path(client.profile['credential_helper']).parents:raise ValueError('Device-local helper required')
        _STARTUP_STAGE='identity-activation'
        client.activate_identity()
    elif args.https_profile_sha256 or args.credential_source_sha256:raise ValueError('HTTPS profile required')
    elif args.ssh_profile:
        _STARTUP_STAGE='ssh-profile-load'
        if any((args.device_plan,args.device_plan_sha256,args.signing_policy,args.signing_policy_sha256,args.component_source_sha256)):raise ValueError('Distinct transport policies required')
        from ssh_content import SSHContentClient
        client=SSHContentClient(args.ssh_profile,args.ssh_profile_sha256)
    else:
        if args.ssh_profile_sha256:raise ValueError('SSH profile required')
        _STARTUP_STAGE='signing-policy-validation'
        signing=pinned(args.signing_policy,args.signing_policy_sha256)
        _STARTUP_STAGE='device-component-load'
        client=ComponentClient(args.device_plan,args.device_plan_sha256,signing)
    _STARTUP_STAGE='transport-policy-validation'
    if bool(args.import_plan)!=bool(args.import_plan_sha256) or args.installation_check and args.import_plan:raise ValueError('Separate exact import approval required')
    if args.installation_check:
        _STARTUP_STAGE='installation-check'
        print(json.dumps(installation_check(client,policy,args.client_version)));return
    _STARTUP_STAGE='content-adapter-start'
    adapter=ContentAdapter(args.device_home,args.cache,policy,client,codex_home=args.codex_home)
    if args.mapping_refresh:
        result=adapter.refresh();print(json.dumps(result))
        if result.get('status') not in ('content-current','content-refreshed'):raise SystemExit(2)
        return
    if args.event_hook:
        _STARTUP_STAGE='event-hook-run'
        run_event_hook(adapter,hook_policy,hook_payload)
        # Hook output is protocol only; never inject captured text or continue a
        # completed turn. Receipt and pending evidence stay in the normal outbox.
        print('{}');return
    if args.import_plan:
        _STARTUP_STAGE='import-plan-validation'
        path=Path(args.import_plan)
        if path.is_symlink() or not path.is_file() or path.stat().st_size>1048576:raise ValueError('Private bounded import plan required')
        private_mode(path)
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=args.import_plan_sha256:raise ValueError('Import plan drift')
        adapter.stage_import(json.loads(raw,object_pairs_hook=pairs))
    _STARTUP_STAGE='mcp-serve'
    serve(ContentServer(adapter,args.client_version),sys.stdin.buffer,sys.stdout)

def cli():
    try:
        # Windows cannot inherit a POSIX descriptor lease. The real worker also
        # holds its own shared LockFileEx lease, including if its launcher dies.
        if os.name=='nt' and '--cache' in sys.argv and '--installation-check' not in sys.argv:
            cache=Path(sys.argv[sys.argv.index('--cache')+1])
            cache_directory(cache)
            with cache_locked(cache/'.content-runtime.lock',timeout=0,shared=True):
                from content_install import inspect
                home=Path(sys.argv[sys.argv.index('--device-home')+1])
                if not inspect(home).get('installed'):
                    raise ValueError('Native installation changed before worker lease')
                main()
        else: main()
    except PlatformUnsupported as error:
        if '--event-hook' in sys.argv:
            print('{}');emit_startup_failure(_STARTUP_STAGE,error);return 0
        print(json.dumps(error.result(served=False,native_changed=False)),file=sys.stderr);return 2
    except Exception as error:
        if '--event-hook' in sys.argv:
            # Some failures happen before staging exists. Report only the
            # bounded stage/category; never claim that local evidence was saved.
            print('{}');emit_startup_failure(_STARTUP_STAGE,error);return 0
        return emit_startup_failure(_STARTUP_STAGE,error)
    return 0


if __name__=='__main__':sys.exit(cli())
