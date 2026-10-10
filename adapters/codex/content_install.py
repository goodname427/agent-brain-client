"""Mac Codex candidate installation: pinned plan, one MCP table, precise recovery.

Preparation is read-only. Apply requires the current private plan's digest and
the caller's deployment authorization. No secret creation, UI, hooks or daemon.
"""
import argparse
import base64
import copy
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import tomllib
from client_release import policy_file,CONTENT_CAPABILITIES,CONTENT_CLIENT_FILES,SSH_CONTENT_CAPABILITIES,SSH_CONTENT_CLIENT_FILES,ClientPackage,ClientReleases
from content_mcp import pinned,pairs
from content_cache import validate_policy,sha,contract
from client_pairing import ComponentClient
from codex_connect import atomic,NAME,BEGIN,END,BLOCK
from brain_lock import locked
from brain_release import ReleaseEngine

class InstallBlocked(ValueError):pass

def safe(path):
    path=Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path,*path.parents)):raise InstallBlocked('Linked/noncanonical path')
    return path

def raw(path,limit=262144):
    path=safe(path)
    if not path.exists():return None
    if not path.is_file() or path.stat().st_size>limit:raise InstallBlocked('Unsupported native file')
    return path.read_bytes()

def save(path,value):atomic(safe(path),contract.encode(value),0o600)
def digest_or_none(value):return sha(value) if value is not None else None
def private_json(path):
    path=safe(path)
    if not path.is_file() or path.stat().st_mode&0o077:raise InstallBlocked('Private metadata required')
    return json.loads(raw(path,1048576),object_pairs_hook=pairs)

def package_check(package,policy):
    package=safe(package)
    if (package/'verified.json').exists():
        receipt=json.loads(raw(package/'verified.json'));manifest=receipt['manifest']
        ReleaseEngine(policy['cache'],policy,None,ClientPackage(policy),{}).validate(manifest,None)
    else:manifest=json.loads(raw(package/'export-manifest.json'))
    if set(manifest['files'])!=set(SSH_CONTENT_CLIENT_FILES if policy['capabilities']==SSH_CONTENT_CAPABILITIES else CONTENT_CLIENT_FILES):raise InstallBlocked('Candidate package membership changed')
    for name,digest in manifest['files'].items():
        if sha(raw(package/name))!=digest:raise InstallBlocked('Candidate package changed')
    ClientPackage(policy).preflight(package,{'files':manifest['files'],'migrations':[]})
    return sha(contract.encode(manifest['files']))

def context(home,package,policy_path,policy_sha):
    if sys.platform!='darwin':raise InstallBlocked('Actual Mac Codex required')
    home=safe(home);policy_path=safe(policy_path);policy=policy_file(policy_path,policy_sha)
    if policy['capabilities'] not in (CONTENT_CAPABILITIES,SSH_CONTENT_CAPABILITIES) or policy['platform']!='darwin-'+platform.machine().lower():raise InstallBlocked('Content release scope/platform required')
    runtime=policy['runtime']
    if runtime['device_home']!=str(home):raise InstallBlocked('Device binding changed')
    native=safe(runtime['codex_home']);cache=safe(runtime['content_cache'])
    if native==cache or native in cache.parents or cache in native.parents or cache==home or any(cache==home/name or home/name in cache.parents for name in ('.agents','.config/agent-brain-client/content')):raise InstallBlocked('Native/cache overlap')
    override=raw(native/'AGENTS.override.md')
    if override and override.strip():raise InstallBlocked('Existing instruction override preserved')
    legacy=home/'.config/agent-brain/device.json';http=home/'.config/agent-brain-client/device.json'
    if http.exists() or http.is_symlink():raise InstallBlocked('Existing HTTP binding needs its migration plan')
    if legacy.exists() or legacy.is_symlink():
        if digest_or_none(raw(legacy))!=runtime.get('legacy_binding_sha256') or digest_or_none(raw(native/'hooks.json'))!=runtime.get('legacy_hooks_sha256'):raise InstallBlocked('Legacy registration/paused hooks need exact preservation review')
    elif 'legacy_binding_sha256' in runtime:raise InstallBlocked('Reviewed preserved legacy binding changed')
    content=pinned(runtime['content_policy'],runtime['content_policy_sha256'])
    if policy['capabilities']==SSH_CONTENT_CAPABILITIES:
        from ssh_content import SSHContentClient
        client=SSHContentClient(runtime['ssh_profile'],runtime['ssh_profile_sha256'])
    else:
        signing=pinned(runtime['signing_policy'],runtime['signing_policy_sha256'])
        client=ComponentClient(runtime['device_plan'],runtime['device_plan_sha256'],signing)
    if client.data_mode!='personal':raise InstallBlocked('Personal content profile required')
    validate_policy(content,client.grant)
    methods=sorted(contract.methods(client.grant['scopes']))
    package_sha=package_check(package,policy)
    mcp={'command':str(Path(sys.executable).resolve()),'args':['-I','-B',str(safe(package)/'adapters/codex/client_startup.py'),'--policy-file',str(policy_path),'--policy-sha256',policy_sha],
         'startup_timeout_sec':60,'tool_timeout_sec':120,'enabled':True,'enabled_tools':methods,'default_tools_approval_mode':'writes'}
    return policy,mcp,package_sha,native

def render(config,mcp,existing):
    text=(config or b'').decode();before=tomllib.loads(text)
    if existing:
        pattern=re.compile(r'^\[mcp_servers\.'+NAME+r'\][ \t]*(?:#.*)?\r?\n',re.M)
        matches=list(pattern.finditer(text))
        if len(matches)!=1:raise InstallBlocked('Existing MCP table syntax needs review')
        start=matches[0].start();end=matches[0].end();next_header=re.search(r'^\[',text[end:],re.M)
        stop=end+next_header.start() if next_header else len(text);text=text[:start]+text[stop:]
    snippet='\n[mcp_servers.'+NAME+']\n'+''.join(k+' = '+json.dumps(v,ensure_ascii=False)+'\n' for k,v in mcp.items())
    desired=(text.rstrip()+'\n'+snippet).encode();expected=copy.deepcopy(before);expected.setdefault('mcp_servers',{})[NAME]=mcp
    if tomllib.loads(desired.decode())!=expected:raise InstallBlocked('Unrelated TOML scope changed')
    return desired

def prepare(home,package,policy_path,policy_sha,replace_existing=False):
    policy,mcp,package_sha,native=context(home,package,policy_path,policy_sha)
    config=native/'config.toml';registration=Path(home)/'.config/agent-brain-client/codex-mcp.json'
    before=raw(config);old=tomllib.loads((before or b'').decode()).get('mcp_servers',{}).get(NAME)
    registered=raw(registration);legacy_agents=None
    if old:
        if not replace_existing or registered is None:raise InstallBlocked('Existing MCP replacement requires a reviewed plan')
        metadata=private_json(registration)
        if metadata.get('schema')!=1 or metadata.get('native_mcp_name')!=NAME:raise InstallBlocked('Unknown existing registration')
        if 'mcp_sha256' in metadata:
            if sha(json.dumps(old,sort_keys=True).encode())!=metadata['mcp_sha256']:raise InstallBlocked('Existing MCP drift')
        else:
            receipt=private_json(metadata['migration_receipt'])
            if receipt.get('installation_complete') is not True or receipt.get('native_registered') is not True or receipt.get('new_registration_path')!=str(registration) or receipt.get('after',{}).get(str(config))!=sha(before) or receipt.get('after',{}).get(str(registration))!=sha(registered):raise InstallBlocked('Existing migration receipt drift')
            agents=native/'AGENTS.md';agents_raw=raw(agents)
            if agents_raw is not None and (BEGIN.encode() in agents_raw or END.encode() in agents_raw):
                text=agents_raw.decode()
                if receipt.get('after',{}).get(str(agents))!=sha(agents_raw) or text.count(BEGIN)!=1 or text.count(END)!=1 or len(BLOCK.findall(text))!=1:raise InstallBlocked('Owned legacy instruction drift')
                after=BLOCK.sub('',text).encode()
                legacy_agents={'path':str(agents),'before_sha256':sha(agents_raw),'after_sha256':sha(after)}
    elif registered is not None:raise InstallBlocked('Orphan registration preserved')
    desired=render(before,mcp,bool(old))
    plan={'schema':1,'adapter':'codex','platform':policy['platform'],'device_home':str(home),'codex_home':str(native),'package':str(package),'package_sha256':package_sha,
          'release_policy':str(policy_path),'release_policy_sha256':policy_sha,'replace_existing':replace_existing,'mcp':mcp,'before':{str(config):digest_or_none(before),str(registration):digest_or_none(registered)},'config_after_sha256':sha(desired),'legacy_agents':legacy_agents}
    if legacy_agents:plan['before'][legacy_agents['path']]=legacy_agents['before_sha256']
    return plan

def probe(plan):
    run=subprocess.run([plan['mcp']['command'],*plan['mcp']['args'],'--installation-check'],capture_output=True,stdin=subprocess.DEVNULL,timeout=60)
    if run.returncode or len(run.stdout)>16384:raise InstallBlocked('Protocol/authentication unconfirmed')
    value=json.loads(run.stdout)
    if value.get('status')!='content-native-protocol-checked' or value.get('protocol_checked') is not True or value.get('authentication_checked') is not True or value.get('native_changed') is not False or value.get('tools')!=plan['mcp']['enabled_tools']:raise InstallBlocked('Unexpected installation protocol check')

def plan_scope(plan):
    keys={'schema','adapter','platform','device_home','codex_home','package','package_sha256','release_policy','release_policy_sha256','replace_existing','mcp','before','config_after_sha256','legacy_agents'}
    if not isinstance(plan,dict) or set(plan)!=keys or plan['schema']!=1 or plan['adapter']!='codex' or type(plan['replace_existing'])!=bool:raise InstallBlocked('Unsupported install plan')
    home,native=safe(plan['device_home']),safe(plan['codex_home'])
    paths={native/'config.toml',home/'.config/agent-brain-client/codex-mcp.json'}
    if plan['legacy_agents'] is not None:
        old=plan['legacy_agents']
        if not isinstance(old,dict) or set(old)!={'path','before_sha256','after_sha256'} or old['path']!=str(native/'AGENTS.md') or plan['before'].get(old['path'])!=old['before_sha256']:raise InstallBlocked('Legacy instruction scope changed')
        paths.add(native/'AGENTS.md')
    if set(plan['before'])!={str(p) for p in paths}:raise InstallBlocked('Native plan scope changed')
    return home,native,paths

def inspect_plan(path,digest):
    plan=pinned(path,digest);home,_,_=plan_scope(plan)
    if sha(contract.encode(plan))!=digest or prepare(home,plan['package'],plan['release_policy'],plan['release_policy_sha256'],plan['replace_existing'])!=plan:raise InstallBlocked('Install plan drift')
    return {'status':'content-native-install-ready','plan_sha256':digest,'native_changed':False,'installed':False,'next_action':'Apply this exact plan within the approved signing, credential, transport and native scope. Protocol/auth checks run before and after the one-table transaction; no new model conversation required.'}

def restore(journal,plan):
    _,_,paths=plan_scope(plan)
    if set(journal)!={'schema','phase','plan_sha256','before','after'} or journal['schema']!=1 or journal['plan_sha256']!=sha(contract.encode(plan)) or set(journal['before'])!={str(p) for p in paths} or set(journal['after'])!=set(journal['before']):raise InstallBlocked('Recovery journal mismatch')
    for path in paths:
        old=journal['before'][str(path)]
        if set(old)!={'data','sha','mode'} or type(old['mode'])!=int or old['mode']&~0o777:raise InstallBlocked('Recovery preimage mismatch')
        backup=base64.b64decode(old['data'],validate=True) if old['data'] is not None else None
        if digest_or_none(backup)!=old['sha'] or digest_or_none(raw(path)) not in (old['sha'],journal['after'][str(path)]):raise InstallBlocked('Outside edit preserved')
    for path in paths:
        old=journal['before'][str(path)]
        if old['data'] is None:path.unlink(missing_ok=True)
        else:atomic(path,base64.b64decode(old['data']),old['mode'])
    journal['phase']='rolled-back'

def apply(plan,approved_digest,check=probe):
    home,native,paths=plan_scope(plan);state=home/'.config/agent-brain-client';receipt=safe(state/'content-install-receipt.json')
    if approved_digest!=sha(contract.encode(plan)):return {'status':'content-install-plan-review-required','native_changed':False,'installed':False}
    with locked(safe(state/'content-install.lock')):
        previous=private_json(receipt) if receipt.exists() else None
        if previous and previous.get('phase') not in ('applying','complete','rolled-back'):raise InstallBlocked('Unknown recovery phase')
        if previous and previous.get('phase')=='complete' and previous.get('plan_sha256')==approved_digest:
            result=inspect(home)
            if result.get('installed'):return result
            raise InstallBlocked('Installed ownership/version changed')
        if previous and previous.get('phase')=='applying':
            try:restore(previous,plan);save(receipt,previous)
            except Exception:return {'status':'content-install-recovery-review-required','native_changed':None,'installed':False}
        current=prepare(home,plan['package'],plan['release_policy'],plan['release_policy_sha256'],plan['replace_existing'])
        if current!=plan:raise InstallBlocked('Install preimage or inputs changed')
        check(plan)  # Read-only protocol/auth preflight before native mutation.
        config=native/'config.toml';registration=state/'codex-mcp.json'
        config_raw=raw(config);desired=render(config_raw,plan['mcp'],config_raw is not None and NAME in tomllib.loads(config_raw.decode()).get('mcp_servers',{}))
        metadata={'schema':1,'native_mcp_name':NAME,'content_candidate':True,'data_mode':'personal-explicit','installation_complete':True,'protocol_checked':True,'model_loaded':False,'platform':plan['platform'],
                  'mcp_sha256':sha(json.dumps(plan['mcp'],sort_keys=True).encode()),'release_policy':plan['release_policy'],'release_policy_sha256':plan['release_policy_sha256'],'package':plan['package'],'package_sha256':plan['package_sha256'],'plan_sha256':approved_digest}
        payload={config:desired,registration:contract.encode(metadata)}
        if plan['legacy_agents']:
            agents=native/'AGENTS.md';cleaned=BLOCK.sub('',raw(agents).decode()).encode()
            if sha(cleaned)!=plan['legacy_agents']['after_sha256']:raise InstallBlocked('Legacy instruction cleanup drift')
            payload[agents]=cleaned
        metadata['owned_install_paths']=sorted(str(p) for p in payload)
        payload[registration]=contract.encode(metadata)
        before={}
        for p in paths:
            original=raw(p);before[str(p)]={'data':base64.b64encode(original).decode() if original is not None else None,'sha':digest_or_none(original),'mode':p.stat().st_mode&0o777 if original is not None else 0o600}
        journal={'schema':1,'phase':'applying','plan_sha256':approved_digest,'before':before,'after':{str(p):sha(v) for p,v in payload.items()}}
        save(receipt,journal)
        try:
            for path,value in payload.items():
                if digest_or_none(raw(path))!=before[str(path)]['sha']:raise InstallBlocked('Preimage race')
                atomic(path,value,before[str(path)]['mode'])
            check(plan);journal['phase']='complete';save(receipt,journal)
        except Exception:
            try:restore(journal,plan);save(receipt,journal)
            except Exception:return {'status':'content-install-recovery-review-required','native_changed':None,'installed':False}
            return {'status':'content-install-rolled-back','native_changed':False,'installed':False}
        return {'status':'content-native-client-installed','native_changed':True,'installed':True,'protocol_checked':True,'model_loaded':False}

def inspect(home):
    try:
        home=safe(home);state=home/'.config/agent-brain-client';registration=state/'codex-mcp.json'
        meta=private_json(registration)
        if meta.get('content_candidate') is not True:return {'status':'not-content-client','native_changed':False}
        policy,mcp,package_sha,native=context(home,meta['package'],meta['release_policy'],meta['release_policy_sha256'])
        receipt=private_json(state/'content-install-receipt.json')
        expected={str(native/'config.toml'),str(registration)}
        if str(native/'AGENTS.md') in meta.get('owned_install_paths',[]):expected.add(str(native/'AGENTS.md'))
        if set(meta.get('owned_install_paths',[]))!=expected or set(receipt.get('after',{}))!=expected:raise InstallBlocked('Receipt scope changed')
        for path,digest in receipt['after'].items():
            current=digest_or_none(raw(path))
            if current==digest:continue
            if path!=str(native/'AGENTS.md'):raise InstallBlocked('Installed ownership drift')
            runtime=policy['runtime'];owned=private_json(Path(runtime['content_cache'])/'owned.json');content=pinned(runtime['content_policy'],runtime['content_policy_sha256'])
            binding={'device_home':str(home),'codex_home':str(native)}
            if set(owned)!={'policy_sha','binding','files'} or owned['binding']!=binding or owned['policy_sha']!=sha(contract.encode({'policy':content,'binding':binding})) or owned['files'].get('.codex/AGENTS.md')!=current or BEGIN.encode() in raw(path) or END.encode() in raw(path):raise InstallBlocked('Mapped instruction ownership drift')
        if receipt.get('phase')!='complete' or receipt.get('plan_sha256')!=meta['plan_sha256'] or package_sha!=meta['package_sha256'] or tomllib.loads(raw(native/'config.toml').decode()).get('mcp_servers',{}).get(NAME)!=mcp:raise InstallBlocked('Installed ownership drift')
        active=ClientReleases(meta['release_policy'],meta['release_policy_sha256']).verified_active()
        if active is None:raise InstallBlocked('Verified client version missing')
        return {'status':'content-native-client-installed','installed':True,'native_changed':False,'protocol_checked':True,'model_loaded':False,'installed_version':active['version'],'update_checkpoint':'approved-MCP-startup'}
    except Exception:return {'status':'content-native-install-review-required','installed':False,'native_changed':False}

def main():
    cli=argparse.ArgumentParser(description=__doc__);cli.add_argument('action',choices=('plan','apply','inspect'))
    for name in ('device-home','package','release-policy','release-policy-sha256','plan-file','plan-sha256','output'):cli.add_argument('--'+name)
    cli.add_argument('--replace-existing',action='store_true');args=cli.parse_args()
    try:
        if args.action=='plan':
            value=prepare(args.device_home,args.package,args.release_policy,args.release_policy_sha256,args.replace_existing);output=safe(args.output)
            if output.exists():raise InstallBlocked('Existing plan preserved')
            save(output,value);result={'status':'content-install-plan-prepared','plan_sha256':sha(contract.encode(value)),'native_changed':False,'installed':False}
        elif args.action=='apply':result=apply(pinned(args.plan_file,args.plan_sha256),args.plan_sha256)
        else:result=inspect(args.device_home)
        print(json.dumps(result));return 0 if result.get('installed') or result['status']=='content-install-plan-prepared' else 2
    except Exception:print(json.dumps({'status':'content-native-install-review-required','installed':False,'native_changed':False,'next_action':'Preserve native files, private plan and recovery receipt; review exact source, binding, grant and ownership. No raw errors or credentials returned.'}));return 2

if __name__=='__main__':sys.exit(main())
