"""Version-one typed content contract; Core memory remains its canonical source.

Rule/Skill records are server-owned JSON, not client-supplied Git paths. The
registry is intentionally small and extensible; clients cannot invent types.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import PurePosixPath
import re

TYPES={'memory','rule','skill'}
FIELDS={'memory_status':{'project_id','session_id'},'memory_resume':{'project_id','session_id'},'memory_stop':{'project_id','session_id'},'memory_revoke':{'project_id','session_id'},'memory_sync':{'project_id','session_id'},'memory_query':{'kind','query','since','until','limit'},'content_read':{'kind','id'},'content_catalog':{'kinds'},
        'memory_event_submit':{'kind','id','event'},
        'content_put':{'kind','id','record','expected_revision'},
        'content_remove':{'kind','id','expected_revision'}}
WRITES={'content_put','content_remove','memory_event_submit'}
HOOK_METHODS={'memory_event_submit'}|{'memory_status','memory_resume','memory_stop','memory_revoke','memory_sync'}
SCOPES={'read','put','write','remove'}

def normalized_scopes(scopes):return {'put' if scope=='write' else scope for scope in scopes}

def permits_scope(scopes,method):
    if method in ('content_put','memory_event_submit'):return 'put' in normalized_scopes(scopes)
    if method=='content_remove':return 'remove' in scopes
    return 'read' in scopes

def methods(scopes):return {name for name in FIELDS if permits_scope(scopes,name)}

def mcp_methods(scopes):return methods(scopes)-HOOK_METHODS

def encode(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()

def validate(method,params):
    if method not in FIELDS or not isinstance(params,dict) or set(params) not in (FIELDS[method],FIELDS[method]|{'context'} if method=='memory_query' else FIELDS[method],FIELDS[method]|{'context','selection'} if method=='memory_query' else FIELDS[method],FIELDS[method]|{'context','source_sessions'} if method=='memory_query' else FIELDS[method],FIELDS[method]|{'context','source_scope'} if method=='memory_query' else FIELDS[method]):raise ValueError('Invalid content request')
    if method in ('memory_status','memory_resume','memory_stop','memory_revoke','memory_sync'):
        for item in params.values():event_identifier(item)
        return
    if method=='memory_query':
        if 'source_scope' in params and params['source_scope']!='approved-project':raise ValueError('Approved project source required')
        if params['kind']!='memory' or not isinstance(params['query'],str) or len(params['query'])>1024 or type(params['limit']) is not int or not 1<=params['limit']<=3:raise ValueError('Bounded Memory query required')
        since,until=query_time(params['since']),query_time(params['until'])
        if since is not None and until is not None and since>until:raise ValueError('Ordered query range required')
        if 'context' in params:
            context=params['context']
            if not isinstance(context,dict) or set(context)!={'project_id','session_id','cross_session'} or context['cross_session'] is not True:raise ValueError('Explicit cross-session recall required')
            for field in ('project_id','session_id'):event_identifier(context[field])
            if not params['query'].strip() or since is not None or until is not None:raise ValueError('Core recall requires text and no legacy time range')
        if 'selection' in params:
            validate_capture_selection(params['selection'])
            if params['limit']!=3 or params['query']!='capture-batches-exact-v1' or params['context']['session_id']==params['selection']['source_session_id']:raise ValueError('Separate exact three-layer recall required')
        if 'source_sessions' in params:
            sources=params['source_sessions']
            if not isinstance(sources,list) or not 1<=len(sources)<=16 or any(not isinstance(s,str) for s in sources) or len(set(sources))!=len(sources) or params['context']['session_id'] in sources:raise ValueError('Distinct finite recall sources required')
            for source in sources:event_identifier(source)
        return
    if method=='content_catalog':
        kinds=params['kinds']
        if not isinstance(kinds,list) or not kinds or any(not isinstance(k,str) or k not in TYPES for k in kinds) or len(set(kinds))!=len(kinds):raise ValueError('Unsupported kinds')
        return
    if not isinstance(params['kind'],str) or params['kind'] not in TYPES or not isinstance(params['id'],str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',params['id']):raise ValueError('Unsupported content identity')
    if method=='memory_event_submit':
        if params['kind']!='memory':raise ValueError('Memory event required')
        event(params['event'])
        if params['id']!=params['event']['event_id']:raise ValueError('Event identity mismatch')
    if method in ('content_put','content_remove') and (not isinstance(params['expected_revision'],str) or not re.fullmatch(r'missing|[a-f0-9]{64}',params['expected_revision'])):raise ValueError('Read revision required')
    if method=='content_put':record(params['kind'],params['record'],params['id'])

def event_identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',value):raise ValueError('Bounded event identity required')

def event(value):
    if not isinstance(value,dict) or set(value)!={'contract','event_id','project_id','session_id','occurred_at','messages'} or value['contract']!='agentbrain.event.v1':raise ValueError('Exact event required')
    for name in ('event_id','project_id','session_id'):event_identifier(value[name])
    stamp=value['occurred_at']
    if not isinstance(stamp,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z',stamp):raise ValueError('UTC event time required')
    query_time(stamp)
    messages=value['messages']
    if not isinstance(messages,list) or not 1<=len(messages)<=100:raise ValueError('Bounded event messages required')
    for message in messages:
        if not isinstance(message,dict) or set(message)!={'role','content'} or message['role'] not in ('user','assistant'):raise ValueError('Event message required')
        text(message['content'],32768)
        if len(message['content'].encode('utf-16-le'))//2>8192:raise ValueError('Event message exceeds bound')
    if len(encode(value))>120000:raise ValueError('Event exceeds bound')

def core_receipt(response,request):
    """Acceptance and a durable receipt ledger are not Core/Git durability."""
    if not isinstance(response,dict) or set(response)!={'request_id','result','persistence'} or response.get('request_id')!=request['request_id']:return False
    receipt=response.get('persistence');value=response.get('result')
    if not isinstance(receipt,dict) or set(receipt)!={'state','durable','github_confirmed','receipt_ledger_durable'} or receipt!={'state':'core-accepted','durable':False,'github_confirmed':False,'receipt_ledger_durable':True}:return False
    if any(type(receipt[name]) is not bool for name in ('durable','github_confirmed','receipt_ledger_durable')):return False
    fields={'event_id','state','message_ids','core_acceptance','ledger_sync','pipeline_state','model_extraction_verified'}
    if not isinstance(value,dict) or set(value) not in (fields,fields|{'replayed'}):return False
    if 'replayed' in value and type(value['replayed']) is not bool:return False
    ids=value['message_ids']
    return (value['event_id']==request['params']['id'] and value['state']=='committed' and
            value['core_acceptance']=='acknowledged-not-durability-proof' and value['ledger_sync']=='sqlite-wal-full' and
            value['pipeline_state']=='unverified' and value['model_extraction_verified'] is False and isinstance(ids,list) and
            len(ids)==len(request['params']['event']['messages']) and all(isinstance(mid,str) and 0<len(mid)<=256 for mid in ids) and len(set(ids))==len(ids))

OWNER_SCOPE='personal-owner'

def personal_owner(ids):
    return isinstance(ids,dict) and set(ids)==TYPES and all(value==OWNER_SCOPE for value in ids.values())

def scope_subset(candidate,ceiling):
    """Typed collection containment; strings never act as ID iterables."""
    for kind,values in candidate.items():
        if kind not in ceiling:return False
        if values==OWNER_SCOPE:
            if not personal_owner(candidate) or not personal_owner(ceiling):return False
        elif values=='*':
            if kind!='memory' or ceiling[kind] not in ('*',OWNER_SCOPE):return False
        elif not isinstance(values,list) or any(not allows(ceiling,kind,identity) for identity in values):return False
    return True

def grant_ids(types,ids):
    if not isinstance(ids,dict) or set(ids)!=set(types):raise ValueError('Exact content IDs required')
    if set(types)-TYPES:raise ValueError('Unsupported content types')
    if any(value==OWNER_SCOPE for value in ids.values()):
        if not personal_owner(ids):raise ValueError('Complete personal owner scope required')
        return
    for kind,values in ids.items():
        if kind=='memory' and values=='*':continue
        if not isinstance(values,list) or len(values)>256 or any(not isinstance(v,str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',v) for v in values) or len(set(values))!=len(values):raise ValueError('Invalid content IDs')

def allows(ids,kind,identity):
    if kind not in TYPES or not isinstance(identity,str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',identity):return False
    values=ids.get(kind,[])
    return personal_owner(ids) or kind=='memory' and values=='*' or isinstance(values,list) and identity in values

def text(value,limit=65536):
    if not isinstance(value,str) or not value.strip() or len(value.encode())>limit or '\0' in value:raise ValueError('Invalid text')

def strings(value,limit=32):
    if not isinstance(value,list) or len(value)>limit or any(not isinstance(v,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.+-]{0,63}',v) for v in value) or len(set(value))!=len(value):raise ValueError('Invalid dependencies')

def skill_frontmatter(value,identity=None):
    front=re.match(r'\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)',value['files']['SKILL.md'],re.S)
    if not front:raise ValueError('Skill frontmatter required')
    def scalar(field):
        found=re.findall(r'^'+field+r':[ \t]*(.*)$',front[1],re.M)
        if len(found)!=1:raise ValueError('Unique Skill metadata required')
        raw=found[0].strip()
        if raw.startswith('"'):raw=json.loads(raw)
        elif raw.startswith("'"):
            if not raw.endswith("'") or len(raw)<2:raise ValueError('Invalid metadata')
            raw=raw[1:-1].replace("''", "'")
        elif raw[:1] in ('|','>','!','&','*','[','{','?') or raw.lower() in ('true','false','null','~','.nan','.inf','-.inf') or re.fullmatch(r'[-+]?\d+(?:\.\d+)?',raw) or re.search(r':\s',raw):raise ValueError('Unverified non-string/multiline/tagged metadata')
        else:raw=re.split(r'[ \t]+#',raw,1)[0].rstrip()
        text(raw,1024);return raw
    name,description=scalar('name'),scalar('description')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',name) or identity is not None and name!=identity or description!=value['description'].strip():raise ValueError('Skill discovery metadata mismatch')

def skill_path(name):
    if not isinstance(name,str) or len(name)>256 or '\\' in name:raise ValueError('Invalid skill path')
    path=PurePosixPath(name)
    if path.is_absolute() or path.as_posix()!=name or any(p.startswith('.') or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]*',p) for p in path.parts):raise ValueError('Unsafe skill path')

def record(kind,value,identity=None):
    if not isinstance(value,dict) or len(encode(value))>65536:raise ValueError('Invalid content record')
    if kind=='memory':
        if set(value)!={'title','description','body','type'} or value['type'] not in ('user','feedback','reference'):raise ValueError('Invalid memory record')
        text(value['title'],1024);text(value['description'],1024);text(value['body'])
    elif kind=='rule':
        if set(value)!={'title','body','enabled','trigger'} or type(value['enabled'])!=bool:raise ValueError('Invalid rule record')
        text(value['title'],1024);text(value['body'])
        trigger=value['trigger']
        if not isinstance(trigger,dict) or set(trigger)!={'mode','patterns'} or trigger['mode'] not in ('always','on-demand','conditional') or not isinstance(trigger['patterns'],list) or len(trigger['patterns'])>32:raise ValueError('Invalid rule trigger')
        for pattern in trigger['patterns']:text(pattern,256)
        if bool(trigger['patterns'])!=(trigger['mode']=='conditional'):raise ValueError('Conditional patterns required')
    elif kind=='skill':
        if set(value)!={'description','enabled','files','dependencies'} or type(value['enabled'])!=bool:raise ValueError('Invalid skill record')
        text(value['description'],1024)
        files=value['files']
        if not isinstance(files,dict) or 'SKILL.md' not in files or not 1<=len(files)<=32:raise ValueError('Skill entry required')
        for name,body in files.items():
            skill_path(name)
            text(body)
        dep=value['dependencies']
        if not isinstance(dep,dict) or set(dep)!={'commands','python_modules'}:raise ValueError('Invalid dependencies')
        strings(dep['commands']);strings(dep['python_modules'])
        if value['enabled']:skill_frontmatter(value,identity)
    else:raise ValueError('Unsupported content kind')

def envelope(kind,identity,value,raw=None):
    return {'content_api_version':1,'kind':kind,'id':identity,'revision':hashlib.sha256(raw if raw is not None else encode(value)).hexdigest() if value is not None else 'missing','record':value}

def path(kind,identity):return 'content/'+kind+'/'+identity+'.json'


ONBOARD_COVERAGE_GUIDANCE = (
    'After installation or reinstallation, once host tools are available, use the currently loaded agent-brain-onboard Skill for source discovery, comparison, approved writes and coverage reporting. '
    'This Client handoff records that no source scan or data transfer has occurred; installation, identity and receipt state do not replace the Skill result. '
)


def installation_follow_up(result):
    """Pure Agent handoff; no source discovery, credentials, model task or upload.

    A prior coverage result is deliberately not inferred from installation or
    identity metadata. The Agent must obtain fresh authorized source evidence.
    """
    value = dict(result)
    if 'model_loaded' in value:
        value['model_loaded'] = 'unknown'
    if value.get('installed') is True:
        value['onboard_check'] = {'status': 'coverage-check-required', 'coverage': 'unknown',
                                 'account_complete': False, 'source_scan_performed': False,
                                 'harness_source_inventory_required': True,
                                 'agentbrain_catalog_is_source_inventory': False,
                                 'data_transferred': False, 'next_action': ONBOARD_COVERAGE_GUIDANCE}
    return value


HOST_GUIDANCE = (
    'AgentBrain is the shared memory service for authorized durable information. Keep Harness-native memory intact and synchronize approved useful items with AgentBrain tools. '
    'Use agent-brain-memory for daily memory work and agent-brain-onboard for authorized source onboarding; report when a relevant Skill is unavailable. '
)


MEMORY_GUIDANCE = (
    'For daily memory decisions, follow the currently loaded agent-brain-memory Skill; for source import and coverage, follow agent-brain-onboard. This Client text defines interface and authorization constraints, not a second behavior policy. '
    'Use only content methods and IDs within the granted scope. Do not infer source access or permission from catalogs, prior imports or local mappings; never access prohibited sessions/databases, secrets or LocalOnly material. '
    'Writes require the current expected_revision and a stable request_id. A local-confirmed receipt with local_durable=true, github_confirmed=false and durable=false confirms the Server saved the write, but not its GitHub backup. Only remote-confirmed with durable=true confirms the GitHub backup; supported conflicts remain pending until reconciled. '
    'A mapping refresh does not prove an already running model reloaded its context. Importing a Rule or Skill does not enable it. '
)


def query_time(value):
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError('ISO8601 time required')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('Explicit time zone required')
    return parsed.astimezone(timezone.utc)


def review_memory_sources(sources,candidates,mode="daily"):
    """Offline policy review of explicit metadata, not collection or authorization.

    Caller classifications are evidence to review, never permission grants or
    automatic language understanding. Bodies/files/credentials are not inputs.
    """
    if mode not in ('daily','onboard'):raise ValueError('Explicit review mode required')
    if not isinstance(sources,list) or not isinstance(candidates,list) or len(sources)>256 or len(candidates)>1024:raise ValueError('Bounded explicit review required')
    index={};counts={};seen=set();rows=[]
    for source in sources:
        if not isinstance(source,dict) or set(source)!={'source_id','status','authorized','expected_candidates','reason'}:raise ValueError('Explicit source coverage required')
        identity=source['source_id'];expected=source['expected_candidates']
        if not isinstance(source['reason'],str) or not 1<=len(source['reason'])<=512 or not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',identity) or identity in index or source['status'] not in ('read','excluded','unavailable','not-checked') or type(source['authorized']) is not bool or expected is not None and (type(expected) is not int or not 0<=expected<=1024):raise ValueError('Invalid source review')
        index[identity]=source;counts[identity]=0
    for candidate in candidates:
        keys={'id','source_id','kind','scope','evidence','utility','sensitive','raw_history','duplicate','requested'}
        if not isinstance(candidate,dict) or set(candidate)!=keys:raise ValueError('Explicit candidate reasons required')
        identity=candidate['id'];source=index.get(candidate['source_id'])
        if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',identity) or identity in seen or source is None or candidate['kind'] not in TYPES or candidate['scope'] not in ('personal-shared','project-only','local-only') or candidate['evidence'] not in ('user-stated','observed-result','inferred') or candidate['utility'] not in ('preference','continuity','useful-fact','reusable-guidance','ephemeral','none') or any(type(candidate[k]) is not bool for k in ('sensitive','raw_history','duplicate','requested')):raise ValueError('Invalid candidate review')
        seen.add(identity);counts[candidate['source_id']]+=1
        if candidate['sensitive'] or candidate['raw_history'] or candidate['scope']=='local-only':decision,reason='excluded','privacy-or-prohibited-history'
        elif not source['authorized'] or source['status']!='read':decision,reason='deferred','source-access-not-authorized-or-unavailable'
        elif candidate['scope']=='project-only':decision,reason='deferred','outside-personal-shared-scope'
        elif candidate['duplicate']:decision,reason='duplicate','equivalent-record-already-present'
        elif mode=='onboard' and candidate['kind']=='memory':decision,reason='candidate','all-authorized-existing-memory-preserve-unique-information-and-provenance'
        elif candidate['requested']:decision,reason='candidate','explicit-user-request-preserve-evidence'
        elif candidate['evidence']=='inferred':decision,reason='review','unverified-inference'
        elif candidate['utility'] in ('ephemeral','none'):decision,reason='skipped','no-useful-future-context'
        else:decision,reason='candidate','useful-authorized-fact-or-continuity'
        rows.append({'id':identity,'source_id':candidate['source_id'],'kind':candidate['kind'],'decision':decision,'reason':reason})
    coverage=[]
    for identity,source in index.items():
        expected=source['expected_candidates'];actual=counts[identity]
        if expected is not None and actual>expected:raise ValueError('Source candidate count mismatch')
        coverage.append(dict(source,reviewed_candidates=actual,fully_reviewed=source['authorized'] and source['status']=='read' and expected is not None and actual==expected))
    totals={name:sum(r['decision']==name for r in rows) for name in ('candidate','duplicate','skipped','excluded','deferred','review')}
    return {'mode':mode,'sources':coverage,'candidates':rows,'counts':totals,'accessible_review_complete':all(r['fully_reviewed'] for r in coverage if r['authorized'] and r['status']=='read'),'coverage_gaps':sum(r['status'] in ('unavailable','not-checked') or r['authorized'] and r['status']=='read' and not r['fully_reviewed'] for r in coverage),'account_complete':False,'data_transferred':False,'permissions_granted':False}


def core_recall(value,context):
    if not isinstance(value,dict) or set(value)!={'content_api_version','method','source','scope','recall'} or value['content_api_version']!=1 or value['method']!='core-recall' or value['source']!='core' or value['scope']!=context:raise ValueError('Unverified Core recall scope')
    recall=value['recall']
    if not isinstance(recall,dict) or set(recall)!={'L0','L1','L2','L3','cross_session','model_extraction_verified'} or recall['cross_session'] is not True or recall['model_extraction_verified'] is not False or len(encode(recall))>240000:raise ValueError('Bounded Core recall required')
    for name in ('L0','L1','L2'):
        if not isinstance(recall[name],list) or len(recall[name])>20 or any(not isinstance(row,dict) or not isinstance(row.get('provenance'),dict) for row in recall[name]):raise ValueError('Bounded sourced Core rows required')
    if not isinstance(recall['L3'],dict) or not isinstance(recall['L3'].get('provenance'),dict) or recall['L3'].get('content') is not None and not isinstance(recall['L3'].get('content'),str):raise ValueError('Sourced Core profile required')


def validate_capture_selection(value):
    """Exact two committed capture batches, filled from runtime receipts only."""
    if not isinstance(value,dict) or set(value)!={'source_session_id','batches'}:raise ValueError('Exact capture selection required')
    identifier=value['source_session_id']
    if not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',identifier):raise ValueError('Source session required')
    batches=value['batches']
    if not isinstance(batches,list) or len(batches)!=2:raise ValueError('Exactly two receipted batches required')
    mids=[];events=[];keys=[]
    for batch in batches:
        if not isinstance(batch,dict) or set(batch)!={'batch_key','event_id','payload_sha256','message_ids','messages_sha256'}:raise ValueError('Exact batch receipt required')
        for field in ('batch_key','payload_sha256','messages_sha256'):
            if not isinstance(batch[field],str) or not re.fullmatch(r'[a-f0-9]{64}',batch[field]):raise ValueError('Runtime digest required')
        event=batch['event_id'];ids=batch['message_ids']
        if not isinstance(event,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',event):raise ValueError('Runtime event required')
        if not isinstance(ids,list) or len(ids)!=2 or any(not isinstance(i,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}',i) for i in ids):raise ValueError('Two runtime message IDs required')
        mids+=ids;events.append(event);keys.append(batch['batch_key'])
    if len(set(mids))!=4 or len(set(events))!=2 or len(set(keys))!=2:raise ValueError('Distinct incremental batches required')
    return value


def exact_capture_response(recall,params,device_id):
    selection=params['selection'];events={b['event_id']:b['payload_sha256'] for b in selection['batches']}
    selection_sha=hashlib.sha256(encode(selection)).hexdigest()
    if recall['L0'] or len(recall['L1'])!=1 or len(recall['L2'])!=1:raise ValueError('Exactly one row per layer required')
    for layer,row in [('L1',recall['L1'][0]),('L2',recall['L2'][0]),('L3',recall['L3'])]:
        p=row['provenance'];sources=p.get('sources',[]);ids=p.get('event_ids',[]);content=row.get('content')
        if (not isinstance(content,str) or not content or p.get('selection_sha256')!=selection_sha
                or p.get('content_sha256')!=hashlib.sha256(content.encode()).hexdigest()
                or p.get('status')!='verified-native-generation-graph' or p.get('model_output_truth_verified') is not False
                or not p.get('generation_ids') or not ids or not set(ids)<=set(events)
                or len(sources)!=len(set(ids)) or {s.get('event_id') for s in sources}!=set(ids)
                or any(s.get('state')!='committed' or s.get('payload_sha256')!=events.get(s.get('event_id'))
                    or s.get('identity',{}).get('device_id')!=device_id
                    or s.get('identity',{}).get('project_id')!=params['context']['project_id']
                    or s.get('identity',{}).get('session_id')!=selection['source_session_id'] for s in sources)
                or layer=='L3' and set(ids)!=set(events)):
            raise ValueError('Unverified exact capture response')
