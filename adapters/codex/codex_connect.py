"""One Mac Codex SSH checkpoint: inspect/reuse, or apply an exact reviewed plan.

No SSH key creation, token copying, hooks, model tests, Core or data Git.
Legacy-only bindings always require their own migration plan.
"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile
import tomllib

NAME='agent_brain_server'
TOOLS=['topic','recall','remember','forget','client_updates']
BEGIN='<!-- agent-brain:begin -->';END='<!-- agent-brain:end -->'
BLOCK=re.compile(re.escape(BEGIN)+r'.*?'+re.escape(END)+r'(?:\r?\n)?',re.S)
MANAGED=BEGIN+'\n# Synthetic test MCP\nUse agent_brain_server only for explicitly requested synthetic tests. Do not automatically query, collect or upload real preferences, facts, rules, memories, project data, native history or credentials. Native memory continues under its existing authorization. Read at most three relevant topics; writes need the read revision and a stable request_id. Only durable=true confirms persistence. Report transport, compatibility and update blockers; do not replace existing permissions or migrate legacy bindings.\n'+END+'\n'
PERSONAL_MANAGED=BEGIN+'\n# Explicit personal memory MCP\nUse agent_brain_server only for explicitly authorized personal shared memory operations. Do not automatically collect or upload conversations, native history, local-only/private material, credentials or project data. Native memory remains available. Read revision before writing and retain request_id on retries; only durable=true confirms persistence. A pinned import manifest is required for migration. Do not infer future collection authorization from scans or previously imported topics.\n'+END+'\n'
sha=lambda data:hashlib.sha256(data).hexdigest()


def profile(path):
    p=Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_mode&0o077:raise ValueError('Private approved profile required')
    value=json.loads(p.read_text())
    if set(value)!={'schema','adapter','platform','data_mode','ssh'} or value['schema']!=1 or value['adapter']!='codex' or value['data_mode'] not in ('synthetic-only','personal-explicit'):raise ValueError('Unsupported service profile')
    if value['platform']!='darwin-'+platform.machine().lower():raise ValueError('Actual Mac Codex platform must match')
    ssh=value['ssh']
    if set(ssh)!={'host','user','identity_file','known_hosts_file','known_hosts_sha256','remote_command'}:raise ValueError('Unknown SSH scope')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}',ssh['host']) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,31}',ssh['user']):raise ValueError('Invalid SSH destination')
    key,known=Path(ssh['identity_file']),Path(ssh['known_hosts_file'])
    if not key.is_absolute() or key.is_symlink() or not key.is_file() or key.stat().st_mode&0o077:raise ValueError('Existing private SSH identity required')
    if not known.is_absolute() or known.is_symlink() or not known.is_file() or sha(known.read_bytes())!=ssh['known_hosts_sha256']:raise ValueError('Approved strict host identity required')
    command=ssh['remote_command']
    if '..' in command or '\n' in command:raise ValueError('Fixed command path escape rejected')
    prefix='/usr/bin/sudo -n -u agent-brain /usr/bin/python3 -I -B '
    bridge=r'/opt/agent-brain-client/current/remote_mcp\.py --endpoint http://127\.0\.0\.1:[0-9]{1,5} --token-file /var/lib/agent-brain/[A-Za-z0-9._/-]+'
    startup=r'/opt/agent-brain-client/[A-Za-z0-9._/-]+/client_startup\.py --policy-file /opt/agent-brain-client/[A-Za-z0-9._/-]+\.json --policy-sha256 [a-f0-9]{64}'
    if not command.startswith(prefix) or not re.fullmatch('(?:'+bridge+'|'+startup+')',command[len(prefix):]):raise ValueError('Fixed reviewed MCP command required')
    args=['-T','-i',str(key)]
    for option in ['IdentitiesOnly=yes','IdentityAgent=none','BatchMode=yes','StrictHostKeyChecking=yes','UserKnownHostsFile='+str(known),'ForwardAgent=no','ClearAllForwardings=yes','ConnectTimeout=5','ServerAliveInterval=30','ServerAliveCountMax=3']:args.extend(['-o',option])
    args.extend([ssh['user']+'@'+ssh['host'],command])
    return value,{'command':'/usr/bin/ssh','args':args,'startup_timeout_sec':30,'tool_timeout_sec':120,'enabled':True,'enabled_tools':TOOLS,'default_tools_approval_mode':'writes'}


def probe(mcp):
    wire=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'codex-client-preflight','version':'1'}}},{'jsonrpc':'2.0','method':'notifications/initialized'}, {'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}},{'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'client_updates','arguments':{}}}]
    run=subprocess.run([mcp['command'],*mcp['args']],input=''.join(json.dumps(v)+'\n' for v in wire),capture_output=True,text=True,timeout=35)
    rows=[json.loads(line) for line in run.stdout.splitlines()]
    if run.returncode or [v['id'] for v in rows]!=[1,2,3] or rows[0]['result']['serverInfo']['name']!='agent-brain-remote-codex' or set(t['name'] for t in rows[1]['result']['tools'])!=set(TOOLS) or rows[2]['result']['isError']:raise ValueError('MCP protocol preflight blocked')
    return rows[2]['result']['structuredContent']['result']


def atomic(path,raw,mode):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(prefix='.client-native-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as file:file.write(raw);file.flush();os.fsync(file.fileno())
        os.chmod(name,mode);os.replace(name,path)
    finally:
        if Path(name).exists():Path(name).unlink()


def connect(home, profile_path=None, apply=False, approved_digest=None):
    home=Path(home).resolve();state=home/'.config/agent-brain-client';registration=state/'codex-mcp.json'
    if os.sys.platform!='darwin':return {'status':'adapter-review-required','native_changed':False,'next_action':'Verify actual Harness loading and prepare its adapter; only Mac Codex is wired here.'}
    native=Path(os.environ.get('CODEX_HOME',str(home/'.codex'))).expanduser().resolve()
    config=native/'config.toml';agents=native/'AGENTS.md';legacy=home/'.config/agent-brain/device.json'
    for p in (registration,config,agents,state,state.parent):
        if p.is_symlink():return {'status':'local-state-review-required','native_changed':False,'next_action':'Preserve linked native/client state and inspect ownership.'}
    override=native/'AGENTS.override.md'
    if override.exists() and override.read_text().strip():return {'status':'native-repair-review-required','native_changed':False,'next_action':'Existing override shadows managed instructions; review before installation or reuse.'}
    cfg=tomllib.loads(config.read_text()) if config.exists() else {}
    if profile_path is None and (state/'service-profile.json').exists():profile_path=state/'service-profile.json'
    candidate,mcp=profile(profile_path) if profile_path else (None,None)
    if registration.exists():
        registered=json.loads(registration.read_text());installed=cfg.get('mcp_servers',{}).get(NAME)
        if not isinstance(registered,dict) or registered.get('schema')!=1 or registered.get('data_mode') not in ('synthetic-only','personal-explicit') or registered.get('real_data_authorized') is not (registered.get('data_mode')=='personal-explicit') or registered.get('native_mcp_name')!=NAME or not installed:
            return {'status':'local-state-review-required','native_changed':False,'next_action':'Preserve unsupported client registration.'}
        if 'mcp_sha256' in registered:
            if legacy.exists():return {'status':'migration-review-required','native_changed':False,'next_action':'A later legacy registration overlaps this client; preserve both and review migration.'}
            text=agents.read_text() if agents.is_file() else ''
            managed=MANAGED if registered['data_mode']=='synthetic-only' else PERSONAL_MANAGED
            valid=sha(json.dumps(installed,sort_keys=True).encode())==registered['mcp_sha256'] and text.count(BEGIN)==text.count(END)==1 and BLOCK.findall(text)==[managed]
        else:
            receipt_path=Path(registered.get('migration_receipt',''))
            valid=False
            if receipt_path.is_file() and not receipt_path.is_symlink() and not receipt_path.stat().st_mode&0o077:
                receipt=json.loads(receipt_path.read_text())
                valid=receipt.get('installation_complete') is True and receipt.get('native_registered') is True and receipt.get('new_registration_path')==str(registration)
                owned={str(config),str(agents),str(native/'hooks.json'),str(home/'.config/agent-brain/codex/permissions-policy.json'),str(home/'.config/agent-brain/codex/integrity.json'),str(registration)}
                valid=valid and set(receipt.get('after',{}))==owned and all(Path(p).is_file() and not Path(p).is_symlink() and sha(Path(p).read_bytes())==expected for p,expected in receipt['after'].items())
        if not valid:return {'status':'native-repair-review-required','native_changed':False,'next_action':'Preserve drifted native files and review the owned migration receipt.'}
        if mcp is not None and (installed!=mcp or registered['data_mode']!=candidate['data_mode']):return {'status':'service-change-review-required','native_changed':False,'next_action':'Review changed SSH identity, fixed command, data mode or destination.'}
        updates=probe(installed)
        return {'status':'native-client-registered','native_changed':False,'protocol_checked':True,'model_loaded':False,'legacy_preserved':legacy.exists(),'updates':updates,'next_action':'Reuse this synthetic MCP. A new release source or runtime bootstrap requires its own pinned approval.'}
    if legacy.exists():return {'status':'migration-review-required','native_changed':False,'next_action':'Prepare an exact legacy migration with backups; preserve existing hooks, bindings and permissions.'}
    if candidate is None:return {'status':'server-authorization-required','native_changed':False,'next_action':'Locate the approved private SSH service profile. Do not send keys or tokens to chat.'}
    if NAME in cfg.get('mcp_servers',{}) or (agents.exists() and (BEGIN in agents.read_text() or END in agents.read_text())):
        return {'status':'native-repair-review-required','native_changed':False,'next_action':'Existing unowned MCP/managed block preserved; inspect its registration.'}
    updates=probe(mcp)
    config_raw=config.read_bytes() if config.exists() else b'';agents_raw=agents.read_bytes() if agents.exists() else b''
    snippet='\n# Explicit synthetic test MCP only\n[mcp_servers.'+NAME+']\n'
    for key,value in mcp.items():snippet+=key+' = '+json.dumps(value,ensure_ascii=False)+'\n'
    managed=MANAGED if candidate['data_mode']=='synthetic-only' else PERSONAL_MANAGED
    new_config=config_raw+b'\n'+snippet.encode();new_agents=agents_raw+(b'\n' if agents_raw and not agents_raw.endswith(b'\n') else b'')+managed.encode()
    expected=copy.deepcopy(cfg);expected.setdefault('mcp_servers',{})[NAME]=mcp
    if tomllib.loads(new_config.decode())!=expected:raise ValueError('Native TOML scope changed')
    plan=sha(json.dumps({'profile':candidate,'before':{str(config):sha(config_raw),str(agents):sha(agents_raw)},'after':{str(config):sha(new_config),str(agents):sha(new_agents)}},sort_keys=True).encode())
    if not apply:return {'status':'ready-to-register','native_changed':False,'plan_sha256':plan,'updates':updates,'next_action':'Apply this exact native plan only within authorization; no credentials, hooks or background tasks are installed.'}
    if approved_digest!=plan:return {'status':'plan-review-required','native_changed':False,'plan_sha256':plan,'next_action':'Approve the exact current native plan digest before applying.'}
    value={'schema':1,'native_mcp_name':NAME,'data_mode':candidate['data_mode'],'real_data_authorized':candidate['data_mode']=='personal-explicit','production_proactive_memory':False,'mcp_sha256':sha(json.dumps(mcp,sort_keys=True).encode()),'platform':candidate['platform'],'model_loaded':False}
    payload={config:new_config,agents:new_agents,registration:(json.dumps(value,indent=2)+'\n').encode()}
    receipt=state/'native-install-receipt.json'
    before={str(p):{'data':base64.b64encode(p.read_bytes()).decode(),'mode':p.stat().st_mode&0o777} if p.exists() else None for p in payload}
    after={str(p):sha(raw) for p,raw in payload.items()};journal={'schema':1,'phase':'prepared','plan_sha256':plan,'before':before,'after':after}
    if receipt.is_symlink():raise ValueError('Linked transaction receipt preserved')
    if receipt.exists():
        previous=json.loads(receipt.read_text())
        if previous.get('phase')!='rolled-back' or previous.get('plan_sha256')!=plan or previous.get('before')!=before:
            return {'status':'native-repair-review-required','native_changed':False,'next_action':'Existing transaction receipt preserved; inspect before retry.'}
    atomic(receipt,(json.dumps(journal)+'\n').encode(),0o600)
    written=[]
    try:
        for p,raw in payload.items():
            old=before[str(p)]
            if p.is_symlink() or (p.read_bytes() if p.exists() else None)!=(base64.b64decode(old['data']) if old else None):raise ValueError('Native preimage changed')
            atomic(p,raw,old['mode'] if old else 0o600);written.append(p)
        journal['phase']='complete';atomic(receipt,(json.dumps(journal)+'\n').encode(),0o600)
    except Exception:
        # Check all postimages before restoring anything; preserve external drift.
        if any(p.is_symlink() or not p.exists() or sha(p.read_bytes())!=after[str(p)] for p in written):raise ValueError('Native rollback conflict preserved') from None
        for p in reversed(written):
            old=before[str(p)]
            if old:atomic(p,base64.b64decode(old['data']),old['mode'])
            else:p.unlink()
        journal['phase']='rolled-back';atomic(receipt,(json.dumps(journal)+'\n').encode(),0o600);raise
    return {'status':'native-client-registered','native_changed':True,'protocol_checked':True,'model_loaded':False,'updates':updates,'next_action':'Use the registered synthetic MCP; configuration and protocol checks completed, no extra model or GUI test required.'}
