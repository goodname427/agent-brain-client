"""Client-only release wiring; pinned local policy, existing release engine.

Catalog discovery never executes downloaded code. Startup updates only immutable
client packages and their pointer; native permissions, credentials and data stay
outside this transaction. No background scheduler or installer is created.
"""
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent if (BASE.parent/'scripts').is_dir() else BASE.parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from brain_release import GitReleaseSource, ReleaseEngine, UpdateBlocked, digest, version
from brain_lock import locked

CLIENT_FILES = ('README.md', 'BOOTSTRAP.md', 'PROTOCOL.md', 'ADAPTERS.md', '.gitignore',
                'connect.py', 'adapters/codex/remote_http.py', 'adapters/codex/remote_mcp.py',
                'adapters/codex/codex_connect.py', 'adapters/codex/client_release.py',
                'adapters/codex/client_startup.py', 'scripts/brain_release.py', 'scripts/brain_lock.py')
CAPABILITIES = ['codex-stdio-mcp', 'synthetic-only']
PERSONAL_CAPABILITIES=['codex-stdio-mcp','personal-memory-explicit']
CONTENT_CLIENT_FILES=CLIENT_FILES+('adapters/codex/client_pairing.py','native/DeviceComponent.swift',
    'adapters/codex/content_cache.py','adapters/codex/content_mcp.py','adapters/codex/content_contract.py','adapters/codex/content_install.py')
CONTENT_CAPABILITIES=['codex-stdio-mcp','personal-content-explicit','signed-device-proxy']
SSH_CONTENT_CLIENT_FILES=tuple(n for n in CONTENT_CLIENT_FILES if n!='native/DeviceComponent.swift')+('adapters/codex/ssh_content.py',)
SSH_CONTENT_CAPABILITIES=['codex-stdio-mcp','personal-content-explicit','ssh-server-credential-proxy']
SSH_CONTENT_RUNTIME={'ssh_profile','ssh_profile_sha256','content_policy','content_policy_sha256','device_home','codex_home','content_cache'}
LEGACY_PRESERVATION={'legacy_binding_sha256','legacy_hooks_sha256'}
CONTENT_RUNTIME={'device_plan','device_plan_sha256','signing_policy','signing_policy_sha256','content_policy','content_policy_sha256','component_source_sha256','device_home','codex_home','content_cache'}


def policy_file(path, expected_sha):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not re.fullmatch(r'[a-f0-9]{64}', expected_sha or ''):
        raise UpdateBlocked('Reviewed release policy and digest required')
    raw = path.read_bytes()
    if len(raw)>65536 or digest(raw)!=expected_sha:
        raise UpdateBlocked('Release policy changed; review required')
    def unique(items):
        value={}
        for key,item in items:
            if key in value:raise UpdateBlocked('Duplicate release policy field')
            value[key]=item
        return value
    policy = json.loads(raw,object_pairs_hook=unique)
    keys = {'schema', 'adapter', 'platform', 'origin', 'ref', 'anchor_commit', 'channel',
            'core_api', 'state_schema', 'capabilities', 'file_prefixes', 'repository', 'cache', 'runtime'}
    if not isinstance(policy,dict) or set(policy)!=keys or policy['schema']!=1:
        raise UpdateBlocked('Unknown client release policy')
    if policy['adapter']!='codex' or policy['platform'] not in ('darwin-arm64', 'darwin-x86_64'):
        raise UpdateBlocked('Only the approved Mac Codex checkpoint is wired')
    if policy['core_api']!=1 or policy['state_schema']!=1 or policy['capabilities'] not in (CAPABILITIES,PERSONAL_CAPABILITIES,CONTENT_CAPABILITIES,SSH_CONTENT_CAPABILITIES):
        raise UpdateBlocked('Client permission/protocol change requires review')
    content=policy['capabilities'] in (CONTENT_CAPABILITIES,SSH_CONTENT_CAPABILITIES)
    ssh=policy['capabilities']==SSH_CONTENT_CAPABILITIES
    if content and path.stat().st_mode&0o077:raise UpdateBlocked('Private content release policy required')
    if policy['channel']!='stable' or policy['file_prefixes']!=list(SSH_CONTENT_CLIENT_FILES if ssh else CONTENT_CLIENT_FILES if content else CLIENT_FILES):
        raise UpdateBlocked('Client release file scope changed')
    origin=policy['origin'];parsed=urlsplit(origin)
    if parsed.scheme:
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise UpdateBlocked('Credential-free HTTPS release origin required')
    elif not Path(origin).is_absolute():
        raise UpdateBlocked('Synthetic local release origin must be pinned and absolute')
    if not re.fullmatch(r'refs/heads/[a-zA-Z0-9][a-zA-Z0-9._/-]*',policy['ref']) or not re.fullmatch(r'[a-f0-9]{40}',policy['anchor_commit']):
        raise UpdateBlocked('Pinned release ref and anchor required')
    for name in ('repository','cache'):
        p=Path(policy[name])
        if not p.is_absolute() or p.is_symlink():raise UpdateBlocked('Approved isolated client paths required')
    repo,cache=map(lambda k:Path(policy[k]).resolve(),('repository','cache'))
    if repo==cache or repo in cache.parents or cache in repo.parents:
        raise UpdateBlocked('Separate client source and package cache required')
    runtime=policy['runtime']
    if content:
        scopes=(SSH_CONTENT_RUNTIME,SSH_CONTENT_RUNTIME|LEGACY_PRESERVATION) if ssh else (CONTENT_RUNTIME,)
        if not isinstance(runtime,dict) or set(runtime) not in scopes:raise UpdateBlocked('Fixed content runtime required')
        for name,value in runtime.items():
            if name.endswith('_sha256'):
                if not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{64}',value):raise UpdateBlocked('Pinned runtime digest required')
            elif not isinstance(value,str) or not Path(value).is_absolute() or Path(value).is_symlink():raise UpdateBlocked('Fixed runtime paths required')
        homes=[Path(runtime[k]).resolve() for k in ('device_home','codex_home','content_cache')]
        if homes[2] in homes[:2] or any(homes[2] in p.parents for p in homes[:2]):raise UpdateBlocked('Separate native/content cache required')
        if any(p==repo or repo in p.parents or p==cache or cache in p.parents for p in homes):raise UpdateBlocked('Native paths must be outside client code paths')
        return policy
    if not isinstance(runtime,dict) or set(runtime)!={'endpoint','token_file'}:
        raise UpdateBlocked('Fixed server runtime destination required')
    # The token remains server-local; validation does not read its contents.
    endpoint=urlsplit(runtime['endpoint'])
    if endpoint.scheme!='http' or endpoint.hostname!='127.0.0.1' or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in ('','/'):
        raise UpdateBlocked('Server bridge requires a fixed loopback API')
    if not Path(runtime['token_file']).is_absolute():raise UpdateBlocked('Server-local token path required')
    return policy


class ClientPackage:
    def __init__(self,policy=None):
        self.content=bool(policy and policy['capabilities'] in (CONTENT_CAPABILITIES,SSH_CONTENT_CAPABILITIES))
        self.ssh=bool(policy and policy['capabilities']==SSH_CONTENT_CAPABILITIES)
        self.files=SSH_CONTENT_CLIENT_FILES if self.ssh else CONTENT_CLIENT_FILES if self.content else CLIENT_FILES
        self.component_source_sha=policy['runtime']['component_source_sha256'] if self.content and not self.ssh else None

    def preflight(self, package, manifest):
        if set(manifest['files'])!=set(self.files) or manifest['migrations']:
            raise UpdateBlocked('Client file membership/migration change requires review')
        if self.content and not self.ssh and manifest['files'].get('native/DeviceComponent.swift')!=self.component_source_sha:raise UpdateBlocked('Native component source changed; signed component/plan review required')
        for name in self.files:
            path=package/name
            if path.is_symlink() or not path.is_file():raise UpdateBlocked('Incomplete client package')
            if name.endswith('.py'):
                try:compile(path.read_bytes(),name,'exec')
                except (SyntaxError,UnicodeError):raise UpdateBlocked('Client syntax check failed') from None

    def snapshot(self):return {}  # Only ReleaseEngine owns the active pointer.
    def refresh(self, package, manifest):pass
    def restore(self, snapshot):pass
    def health(self, package, manifest):
        entry='adapters/codex/content_mcp.py' if self.content else 'connect.py'
        result=subprocess.run([sys.executable,'-I','-B',str(package/entry),'--self-check'],
                              capture_output=True,stdin=subprocess.DEVNULL,timeout=15)
        expected={'content_mcp_self_check':'passed','core_imported':False,'native_changed':False,'keychain_accessed':False,'network_used':False} if self.content else {'client_self_check':'passed','core_imported':False,'native_changed':False}
        try:healthy=json.loads(result.stdout)==expected
        except (ValueError,UnicodeError):healthy=False
        if result.returncode or not healthy:raise UpdateBlocked('Client health check failed; previous package restored')


class ClientReleases:
    def __init__(self, path, expected_sha):
        self.policy=policy_file(path,expected_sha)
        repo=Path(self.policy['repository'])
        if not (repo/'.git').is_dir():raise UpdateBlocked('Approved client-only release checkout required; no silent clone')
        # Bound remote discovery inside the existing 30s MCP startup budget.
        self.engine=ReleaseEngine(self.policy['cache'],self.policy,GitReleaseSource(repo,self.policy,timeout=5),ClientPackage(self.policy),{})

    def verified_active(self):
        active=self.engine.active()
        if active and self.policy['capabilities'] in (CONTENT_CAPABILITIES,SSH_CONTENT_CAPABILITIES):
            receipt=json.loads((Path(active['package'])/'verified.json').read_text())
            self.engine.adapter.preflight(Path(active['package']),receipt['manifest'])
        return active

    def discover(self, adapter, platform, installed_version):
        version(installed_version)
        if (adapter,platform)!=(self.policy['adapter'],self.policy['platform']):
            return {'status':'adapter-review-required','releases':[],'next_action':'Use the approved Mac Codex checkpoint; prepare grounded loading evidence for any other Harness.'}
        try:
            with locked(self.engine.local/'update.lock'),locked(self.engine.local.parent/'python-write.lock'):
                commit,manifest,read=self.engine.source.latest()
                self.engine.validate(manifest,{'version':installed_version})
                if set(manifest['files'])!=set(self.engine.adapter.files) or manifest['migrations']:
                    raise UpdateBlocked('Client release scope changed')
                package=self.engine.prepare(commit,manifest,read)
                self.engine.adapter.preflight(package,manifest)
            return {'status':'current' if manifest['version']==installed_version else 'compatible-update',
                    'releases':[{'commit':commit,'manifest':manifest,'adapter':adapter,'platform':platform}],
                    'next_action':'Check the already pinned client policy at the approved MCP startup. Discovery never grants a new release source or installs code.'}
        except UpdateBlocked:
            return {'status':'release-review-required','releases':[], 'next_action':'Review source ancestry, file hashes, capabilities, protocol, dependencies and version. Keep the current client.'}
        except (OSError,ValueError,KeyError,TypeError,RuntimeError,subprocess.TimeoutExpired):
            return {'status':'release-unavailable','releases':[], 'next_action':'Restore access to the pinned release checkout; keep the current client and do not copy credentials.'}

    def update(self):
        try:
            result=self.engine.check()
            result['active']=self.verified_active()
            result['next_action']='Use the verified active client; native permissions and data are unchanged.'
            return result
        except (UpdateBlocked,OSError,ValueError,KeyError,TypeError,subprocess.TimeoutExpired):
            # Never echo upstream paths, credentials, raw stderr or exception values.
            try:active=self.verified_active()
            except (UpdateBlocked,OSError,ValueError,KeyError,TypeError):active=None
            return {'status':'release-review-required','active':active,
                    'next_action':'Inspect the pinned release policy/package or failed health check; prior activation is retained or restored.'}
