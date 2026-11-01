"""Mac Codex candidate installation: pinned plan, one MCP table, precise recovery.

Preparation is read-only. Apply requires the current private plan's digest and
the caller's deployment authorization. No secret creation, UI, hooks or daemon.
"""
import argparse
import base64
import copy
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tomllib
from client_release import https_files,policy_file,CONTENT_CAPABILITIES,CONTENT_CLIENT_FILES,SSH_CONTENT_CAPABILITIES,SSH_CONTENT_CLIENT_FILES,HTTPS_CONTENT_CAPABILITIES,HTTPS_CONTENT_CLIENT_FILES,LEGACY_030_HTTPS_CONTENT_CLIENT_FILES,ClientPackage,ClientReleases,dependency_environment
from content_mcp import pinned,pairs
from content_cache import validate_policy,sha,contract
from client_pairing import ComponentClient
from codex_connect import atomic,NAME,BEGIN,END,BLOCK
from brain_lock import locked,require_platform,platform_paths,private_mode,PlatformUnsupported,native_platform
from brain_release import ReleaseEngine

class InstallBlocked(ValueError):pass

def safe(path):
    path=Path(path)
    if os.name == 'nt':
        from brain_windows import canonical
        canonical(path)
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
    require_platform('private-metadata')
    path=safe(path)
    private_mode(path,blocked=InstallBlocked)
    return json.loads(raw(path,1048576),object_pairs_hook=pairs)

def package_check(package,policy,*,allow_legacy_030_active=False):
    package=safe(package)
    if (package/'verified.json').exists():
        receipt=json.loads(raw(package/'verified.json'));manifest=receipt['manifest']
        ReleaseEngine(policy['cache'],policy,None,ClientPackage(policy),{}).validate(manifest,None)
    else:manifest=json.loads(raw(package/'export-manifest.json'))
    files=https_files(policy['platform']) if policy['capabilities']==HTTPS_CONTENT_CAPABILITIES else SSH_CONTENT_CLIENT_FILES if policy['capabilities']==SSH_CONTENT_CAPABILITIES else CONTENT_CLIENT_FILES
    legacy_030_active=(allow_legacy_030_active and policy['capabilities']==HTTPS_CONTENT_CAPABILITIES
                       and policy['file_prefixes']==list(LEGACY_030_HTTPS_CONTENT_CLIENT_FILES)
                       and manifest.get('version')=='0.3.0'
                       and set(manifest['files'])==set(LEGACY_030_HTTPS_CONTENT_CLIENT_FILES))
    if legacy_030_active:files=LEGACY_030_HTTPS_CONTENT_CLIENT_FILES
    if set(manifest['files'])!=set(files):raise InstallBlocked('Candidate package membership changed')
    for name,digest in manifest['files'].items():
        if sha(raw(package/name))!=digest:raise InstallBlocked('Candidate package changed')
    ClientPackage(policy).preflight(package,{'version':manifest.get('version'),'files':manifest['files'],'migrations':[]},allow_legacy_030_active=legacy_030_active)
    return sha(contract.encode(manifest['files']))

def context(home,package,policy_path,policy_sha,*,inspection=False,allow_legacy_030_active=False):
    require_platform('native-install')
    home=safe(home);policy_path=safe(policy_path);policy=policy_file(policy_path,policy_sha,runtime_check=not inspection)
    if policy['capabilities'] not in (CONTENT_CAPABILITIES,SSH_CONTENT_CAPABILITIES,HTTPS_CONTENT_CAPABILITIES) or policy['platform']!=native_platform():raise InstallBlocked('Content release scope/platform required')
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
    if policy['capabilities']==HTTPS_CONTENT_CAPABILITIES:
        from https_content import HTTPSContentClient
        client=HTTPSContentClient(runtime['https_profile'],runtime['https_profile_sha256'],credential_access=False)
        if (client.profile.get('credential_source_sha256') or client.profile['credential_backend']['source_sha256'])!=runtime['credential_source_sha256'] or Path(client.profile['state_directory'])!=home/'.config/agent-brain-client/https':raise InstallBlocked('HTTPS credential runtime binding changed')
        if 'credential_helper' in client.profile:
            helper=Path(client.profile['credential_helper'])
            if home not in helper.parents:raise InstallBlocked('Reviewed device-local credential helper required')
    elif policy['capabilities']==SSH_CONTENT_CAPABILITIES:
        from ssh_content import SSHContentClient
        client=SSHContentClient(runtime['ssh_profile'],runtime['ssh_profile_sha256'])
    else:
        signing=pinned(runtime['signing_policy'],runtime['signing_policy_sha256'])
        client=ComponentClient(runtime['device_plan'],runtime['device_plan_sha256'],signing)
    if client.data_mode!='personal':raise InstallBlocked('Personal content profile required')
    validate_policy(content,client.grant)
    methods=sorted(contract.mcp_methods(client.grant['scopes'])-{'memory_query'})
    package_sha=package_check(package,policy,allow_legacy_030_active=allow_legacy_030_active)
    mcp={'command':str(Path(sys.executable).resolve()),'args':['-I','-B',str(safe(package)/'adapters/codex/client_startup.py'),'--policy-file',str(policy_path),'--policy-sha256',policy_sha],
         'startup_timeout_sec':60,'tool_timeout_sec':120,'enabled':True,'enabled_tools':methods,'default_tools_approval_mode':'writes'}
    environment=dependency_environment(policy,runtime_check=not inspection)
    if environment is not None:mcp['env']=environment
    return policy,mcp,package_sha,native

def render(config,mcp,existing):
    text=(config or b'').decode();before=tomllib.loads(text)
    if existing:
        pattern=re.compile(r'^\[mcp_servers\.'+NAME+r'\][ \t]*(?:#.*)?\r?\n',re.M)
        matches=list(pattern.finditer(text))
        if len(matches)!=1:raise InstallBlocked('Existing MCP table syntax needs review')
        start=matches[0].start();end=matches[0].end();next_header=re.search(r'^\[',text[end:],re.M)
        stop=end+next_header.start() if next_header else len(text);text=text[:start]+text[stop:]
    def literal(value):
        if isinstance(value,dict):return '{'+', '.join(json.dumps(k)+' = '+literal(value[k]) for k in sorted(value))+'}'
        return json.dumps(value,ensure_ascii=False)
    snippet='\n[mcp_servers.'+NAME+']\n'+''.join(k+' = '+literal(mcp[k])+'\n' for k in sorted(mcp))
    desired=(text.rstrip()+'\n'+snippet).encode();expected=copy.deepcopy(before);expected.setdefault('mcp_servers',{})[NAME]=mcp
    if tomllib.loads(desired.decode())!=expected:raise InstallBlocked('Unrelated TOML scope changed')
    return desired


def registered_context(home,metadata,config_image=None):
    """Verify the original pinned command, independently of the inspecting Python.

    Prepare still selects the installing interpreter. Inspection/removal uses
    the complete registered table's pinned digest and checks its executable;
    changing command/args/env never becomes acceptable by running another Python.
    config_image is only an uninstall journal's validated original config image.
    """
    policy,mcp,package_sha,native=context(home,metadata['package'],metadata['release_policy'],metadata['release_policy_sha256'],inspection=True,allow_legacy_030_active=True)
    recorded=tomllib.loads((raw(native/'config.toml') if config_image is None else config_image).decode()).get('mcp_servers',{}).get(NAME)
    if not isinstance(recorded,dict) or registered_mcp_digest(recorded)!=metadata.get('mcp_sha256'):
        raise InstallBlocked('Installed MCP binding drift')
    command=recorded.get('command')
    if not isinstance(command,str):raise InstallBlocked('Installed interpreter unavailable')
    executable=safe(command)
    if str(executable.resolve())!=command or not executable.is_file() or not os.access(executable,os.X_OK):
        raise InstallBlocked('Installed interpreter unavailable')
    if 'dependency_policy' in policy['runtime']:
        dependencies=private_json(policy['runtime']['dependency_policy'])
        if command!=str(Path(dependencies['python']).resolve()):raise InstallBlocked('Installed interpreter dependency binding changed')
    mcp['command']=command
    if recorded.get('enabled') is False:mcp['enabled']=False
    if recorded!=mcp:raise InstallBlocked('Installed MCP binding drift')
    return policy,mcp,package_sha,native


def registered_mcp_digest(mcp):
    # A host may pause this exact service before owned removal. Authenticate the
    # entire original table, accepting only enabled True -> False (never expansion).
    value=dict(mcp,enabled=True) if mcp.get('enabled') is False else mcp
    return sha(json.dumps(value,sort_keys=True).encode())


INSPECTION_REASONS={
    'Installed MCP binding drift':('mcp-binding-changed','Preserve config and registration. Restore the original pinned Agent Brain table or obtain a reviewed replacement plan; do not edit its digest.'),
    'Installed interpreter unavailable':('interpreter-unavailable','Restore the originally registered Python at its original canonical path, or obtain a reviewed replacement plan. Do not redirect the registered command.'),
    'Installed interpreter dependency binding changed':('interpreter-policy-mismatch','Preserve the dependency policy and registration; review the original Python binding instead of substituting the inspecting interpreter.'),
    'Candidate package changed':('bootstrap-package-changed','Preserve the fixed bootstrap and receipt. Restore the exact verified package or obtain a reviewed replacement; do not rewrite its manifest.'),
    'Candidate package membership changed':('bootstrap-package-scope-changed','Preserve the fixed bootstrap; review its exact file membership without expanding the release policy.'),
    'Receipt scope changed':('install-receipt-scope-changed','Preserve registration and receipt. Review the exact transaction scope before recovery.'),
    'Installed ownership drift':('installation-ownership-changed','Preserve native files and install receipt. Compare the pinned registration/package and transaction images; do not reset or reinstall over unknown state.'),
    'Mapped instruction ownership drift':('instruction-mapping-changed','Preserve user instructions and the content cache. Review the owned mapping before removal; no user edits are overwritten.'),
}

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
            if registered_mcp_digest(old)!=metadata['mcp_sha256']:raise InstallBlocked('Existing MCP drift')
            if old.get('enabled') is False:mcp['enabled']=False
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
    environment=None
    if 'env' in plan['mcp']:environment=dict(plan['mcp']['env'],HOME=plan['device_home'],LANG='en_US.UTF-8')
    run=subprocess.run([plan['mcp']['command'],*plan['mcp']['args'],'--installation-check'],capture_output=True,stdin=subprocess.DEVNULL,timeout=60,env=environment)
    if run.returncode or len(run.stdout)>16384:raise InstallBlocked('Protocol/authentication unconfirmed')
    value=json.loads(run.stdout)
    if value.get('status')!='content-native-protocol-checked' or value.get('protocol_checked') is not True or value.get('authentication_checked') is not True or value.get('native_changed') is not False or value.get('tools')!=plan['mcp']['enabled_tools']:raise InstallBlocked('Unexpected installation protocol check')


def probe_bootstrap(plan):
    """Verify a candidate launcher against the still-active package offline."""
    environment=None
    if 'env' in plan['mcp']:environment=dict(plan['mcp']['env'],HOME=plan['device_home'],LANG='en_US.UTF-8')
    command=[plan['mcp']['command'],*plan['mcp']['args'],'--bootstrap-check']
    run=subprocess.run(command,capture_output=True,stdin=subprocess.DEVNULL,timeout=30,env=environment)
    if run.returncode or len(run.stdout)>16384:raise InstallBlocked('Bootstrap handover check failed')
    value=json.loads(run.stdout)
    if (value.get('status')!='client-bootstrap-verified'
            or value.get('active_package_version')!='0.3.0'
            or value.get('native_changed') is not False
            or value.get('network_used') is not False
            or value.get('device_credentials_accessed') is not False):
        raise InstallBlocked('Unexpected bootstrap handover check')

def validate_install_scope(plan):
    keys={'schema','adapter','platform','device_home','codex_home','package','package_sha256','release_policy','release_policy_sha256','replace_existing','mcp','before','config_after_sha256','legacy_agents'}
    if not isinstance(plan,dict) or set(plan)!=keys or plan['schema']!=1 or plan['adapter']!='codex' or type(plan['replace_existing'])!=bool:raise InstallBlocked('Unsupported install plan')
    path_type=platform_paths(plan['platform'])
    home,native=path_type(plan['device_home']),path_type(plan['codex_home'])
    if any(not p.is_absolute() or '..' in p.parts for p in (home,native)):raise InstallBlocked('Absolute native paths required')
    paths={native/'config.toml',home/'.config/agent-brain-client/codex-mcp.json'}
    if plan['legacy_agents'] is not None:
        old=plan['legacy_agents']
        if not isinstance(old,dict) or set(old)!={'path','before_sha256','after_sha256'} or old['path']!=str(native/'AGENTS.md') or plan['before'].get(old['path'])!=old['before_sha256']:raise InstallBlocked('Legacy instruction scope changed')
        paths.add(native/'AGENTS.md')
    if set(plan['before'])!={str(p) for p in paths}:raise InstallBlocked('Native plan scope changed')
    return home,native,paths

def plan_scope(plan):
    require_platform('native-install',plan.get('platform') if isinstance(plan,dict) else None)
    home,native,paths=validate_install_scope(plan)
    return safe(str(home)),safe(str(native)),{safe(str(p)) for p in paths}


def inspect_plan(path,digest):
    plan=pinned(path,digest);home,_,_=plan_scope(plan)
    if sha(contract.encode(plan))!=digest or prepare(home,plan['package'],plan['release_policy'],plan['release_policy_sha256'],plan['replace_existing'])!=plan:raise InstallBlocked('Install plan drift')
    return {'status':'content-native-install-ready','plan_sha256':digest,'native_changed':False,'installed':False,'next_action':'Apply this exact plan within the approved signing, credential, transport and native scope. Protocol/auth checks run before and after the one-table transaction; no new model conversation required.'}

def validate_recovery(journal,plan):
    _,_,paths=plan_scope(plan)
    if (set(journal)!={'schema','phase','plan_sha256','before','after'}
            or journal['schema']!=1 or journal['phase']!='applying'
            or journal['plan_sha256']!=sha(contract.encode(plan))
            or set(journal['before'])!={str(p) for p in paths}
            or set(journal['after'])!=set(journal['before'])):raise InstallBlocked('Recovery journal mismatch')
    for path in paths:
        old=journal['before'][str(path)]
        if set(old)!={'data','sha','mode'} or type(old['mode'])!=int or old['mode']&~0o777:raise InstallBlocked('Recovery preimage mismatch')
        backup=base64.b64decode(old['data'],validate=True) if old['data'] is not None else None
        if digest_or_none(backup)!=old['sha'] or digest_or_none(raw(path)) not in (old['sha'],journal['after'][str(path)]):raise InstallBlocked('Outside edit preserved')
    return paths

def restore(journal,plan):
    _,_,paths=plan_scope(plan)
    validate_recovery(journal,plan)
    for path in paths:
        old=journal['before'][str(path)]
        if old['data'] is None:path.unlink(missing_ok=True)
        else:atomic(path,base64.b64decode(old['data']),old['mode'])
    journal['phase']='rolled-back'

def apply(plan,approved_digest,check=probe):
    try:require_platform('native-install',plan.get('platform') if isinstance(plan,dict) else None)
    except PlatformUnsupported as error:return error.result(installed=False,native_changed=False)
    home,native,paths=plan_scope(plan);state=home/'.config/agent-brain-client';receipt=safe(state/'content-install-receipt.json')
    if approved_digest!=sha(contract.encode(plan)):return {'status':'content-install-plan-review-required','native_changed':False,'installed':False}
    with locked(safe(state/'content-install.lock')):
        receipt_preimage=raw(receipt,1048576)
        if receipt_preimage is not None:
            private_mode(receipt,blocked=InstallBlocked)
            previous=json.loads(receipt_preimage,object_pairs_hook=pairs)
        else:previous=None
        # A removal keeps the old install journal for recovery. Reinstall must
        # run a fresh transaction even when the new plan has identical bytes.
        removed=safe(state/'uninstall-receipt.json')
        reinstallation=removed.exists() and not (state/'codex-mcp.json').exists()
        if previous and previous.get('phase') not in ('applying','complete','rolled-back'):raise InstallBlocked('Unknown recovery phase')
        if previous and previous.get('phase')=='complete' and previous.get('plan_sha256')==approved_digest and not reinstallation:
            result=inspect(home)
            if result.get('installed'):return result
            raise InstallBlocked('Installed ownership/version changed')
        if previous and previous.get('phase')=='applying':
            try:restore(previous,plan);save(receipt,previous)
            except Exception:return {'status':'content-install-recovery-review-required','native_changed':None,'installed':False}
            receipt_preimage=raw(receipt,1048576)
        current=prepare(home,plan['package'],plan['release_policy'],plan['release_policy_sha256'],plan['replace_existing'])
        if current!=plan:raise InstallBlocked('Install preimage or inputs changed')
        check(plan)  # Read-only protocol/auth preflight before native mutation.
        config=native/'config.toml';registration=state/'codex-mcp.json'
        config_raw=raw(config);desired=render(config_raw,plan['mcp'],config_raw is not None and NAME in tomllib.loads(config_raw.decode()).get('mcp_servers',{}))
        if sha(desired)!=plan['config_after_sha256']:raise InstallBlocked('Config rendering differs from approved plan')
        metadata={'schema':1,'native_mcp_name':NAME,'content_candidate':True,'data_mode':'personal-explicit','installation_complete':True,'protocol_checked':True,'model_loaded':'unknown','platform':plan['platform'],
                  'mcp_sha256':registered_mcp_digest(plan['mcp']),'release_policy':plan['release_policy'],'release_policy_sha256':plan['release_policy_sha256'],'package':plan['package'],'package_sha256':plan['package_sha256'],'plan_sha256':approved_digest}
        payload={config:desired,registration:contract.encode(metadata)}
        if plan['legacy_agents']:
            agents=native/'AGENTS.md';cleaned=BLOCK.sub('',raw(agents).decode()).encode()
            if sha(cleaned)!=plan['legacy_agents']['after_sha256']:raise InstallBlocked('Legacy instruction cleanup drift')
            payload[agents]=cleaned
        metadata['owned_install_paths']=sorted(str(p) for p in payload)
        payload[registration]=contract.encode(metadata)
        before={}
        for p in paths:
            original=raw(p);original_sha=digest_or_none(original)
            if original_sha!=plan['before'][str(p)]:raise InstallBlocked('Preimage race')
            before[str(p)]={'data':base64.b64encode(original).decode() if original is not None else None,'sha':original_sha,'mode':p.stat().st_mode&0o777 if original is not None else 0o600}
        current_receipt=raw(receipt,1048576)
        if current_receipt!=receipt_preimage:raise InstallBlocked('Install receipt changed before commit')
        if {name:value['sha'] for name,value in before.items()}!=plan['before']:
            raise InstallBlocked('Install preimage changed before commit')
        journal={'schema':1,'phase':'applying','plan_sha256':approved_digest,'before':before,'after':{str(p):sha(v) for p,v in payload.items()}}
        save(receipt,journal)
        try:
            for path,value in payload.items():
                if digest_or_none(raw(path))!=before[str(path)]['sha']:raise InstallBlocked('Preimage race')
                atomic(path,value,before[str(path)]['mode'])
            check(plan)
            completed=dict(journal,phase='complete')
            save(receipt,completed)
            journal['phase']='complete'
        except Exception:
            try:
                restore(journal,plan)
                if previous and previous.get('phase')=='complete' and not reinstallation:save(receipt,previous)
                else:save(receipt,journal)
            except Exception:return {'status':'content-install-recovery-review-required','native_changed':None,'installed':False}
            return {'status':'content-install-rolled-back','native_changed':False,'installed':False}
        return {'status':'content-native-client-installed','native_changed':True,'installed':True,'protocol_checked':True,'model_loaded':'unknown'}

def inspect(home):
    present=False;stage='installation-path-review-required'
    try:
        require_platform('native-install')
        home=safe(home);state=home/'.config/agent-brain-client';registration=state/'codex-mcp.json'
        present=registration.exists() or registration.is_symlink()
        if not present:
            if (state/'content-install-receipt.json').exists():
                return {'status':'content-native-install-review-required','installed':False,'installation_present':False,'installation_state':'registration-missing-with-recovery-materials','reason':'registration-missing','native_changed':False,
                        'next_action':'Preserve the install receipt and local data. Resume the original install command; for a completed uninstall use the public lifecycle status.'}
            return {'status':'client-not-installed','installed':False,'installation_present':False,'installation_state':'absent','native_changed':False}
        stage='registration-metadata-review-required'
        meta=private_json(registration)
        if meta.get('content_candidate') is not True:return {'status':'not-content-client','installed':False,'installation_present':True,'installation_state':'other-registration','native_changed':False,'reason':'unsupported-registration'}
        stage='policy-profile-bootstrap-review-required'
        policy,mcp,package_sha,native=registered_context(home,meta)
        stage='install-receipt-review-required'
        receipt=private_json(state/'content-install-receipt.json')
        expected={str(native/'config.toml'),str(registration)}
        if str(native/'AGENTS.md') in meta.get('owned_install_paths',[]):expected.add(str(native/'AGENTS.md'))
        if set(meta.get('owned_install_paths',[]))!=expected or set(receipt.get('after',{}))!=expected:raise InstallBlocked('Receipt scope changed')
        for path,digest in receipt['after'].items():
            current=digest_or_none(raw(path))
            if current==digest:continue
            if path==str(native/'config.toml'):
                # The transaction journal owns full byte images for recovery;
                # runtime ownership covers only our exact MCP table. Codex may
                # legitimately update model, UI or other unrelated settings.
                current_mcp=tomllib.loads(raw(path).decode()).get('mcp_servers',{}).get(NAME)
                if current_mcp!=mcp or meta.get('mcp_sha256')!=registered_mcp_digest(current_mcp):raise InstallBlocked('Installed MCP binding drift')
                continue
            if path!=str(native/'AGENTS.md'):raise InstallBlocked('Installed ownership drift')
            runtime=policy['runtime'];owned=private_json(Path(runtime['content_cache'])/'owned.json');content=pinned(runtime['content_policy'],runtime['content_policy_sha256'])
            binding={'device_home':str(home),'codex_home':str(native)}
            if set(owned)!={'policy_sha','binding','files'} or owned['binding']!=binding or owned['policy_sha']!=sha(contract.encode({'policy':content,'binding':binding})) or owned['files'].get('.codex/AGENTS.md')!=current or BEGIN.encode() in raw(path) or END.encode() in raw(path):raise InstallBlocked('Mapped instruction ownership drift')
        if receipt.get('phase')!='complete' or receipt.get('plan_sha256')!=meta['plan_sha256'] or package_sha!=meta['package_sha256'] or meta.get('mcp_sha256')!=registered_mcp_digest(mcp) or tomllib.loads(raw(native/'config.toml').decode()).get('mcp_servers',{}).get(NAME)!=mcp:raise InstallBlocked('Installed ownership drift')
        stage='active-release-review-required'
        active=ClientReleases(meta['release_policy'],meta['release_policy_sha256'],inspection=True).verified_active()
        if active is None:raise InstallBlocked('Verified client version missing')
        enabled=mcp.get('enabled') is True
        return {'status':'content-native-client-installed' if enabled else 'content-native-client-disabled','installed':enabled,'registration_verified':True,'installation_present':True,'installation_state':'verified' if enabled else 'verified-disabled','native_changed':False,'protocol_checked':True,'model_loaded':'unknown','installed_version':active['version'],'update_checkpoint':'approved-MCP-startup'}
    except PlatformUnsupported as error:return error.result(installed=False,native_changed=False)
    except Exception as error:
        reason,action=INSPECTION_REASONS.get(str(error), (stage,'Preserve native files, registration and recovery receipts. Review the named local integrity checkpoint; do not delete state, rewrite digests or reset Keychain.'))
        return {'status':'content-native-install-review-required','installed':False,'installation_present':present,'installation_state':'present-review-required' if present else 'path-review-required','native_changed':False,'reason':reason,'next_action':action}

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
    except PlatformUnsupported as error:print(json.dumps(error.result(installed=False,native_changed=False)));return 2
    except Exception:print(json.dumps({'status':'content-native-install-review-required','installed':False,'native_changed':False,'next_action':'Preserve native files, private plan and recovery receipt; review exact source, binding, grant and ownership. No raw errors or credentials returned.'}));return 2

if __name__=='__main__':sys.exit(main())
