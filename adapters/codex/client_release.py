"""Client-only release wiring; pinned local policy, existing release engine.

Catalog discovery never executes downloaded code. Startup updates only immutable
client packages and their pointer; native permissions, credentials and data stay
outside this transaction. No background scheduler or installer is created.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import sys
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent if (BASE.parent/'scripts').is_dir() else BASE.parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
from brain_release import GitReleaseSource, ReleaseEngine, UpdateBlocked, digest, version
from brain_lock import locked,require_platform,platform_paths,WINDOWS_PLATFORMS,private_mode,PlatformUnsupported,LockTimeout

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
HTTPS_CONTENT_CLIENT_FILES=tuple(n for n in CONTENT_CLIENT_FILES if n!='native/DeviceComponent.swift')+('adapters/codex/https_content.py','adapters/codex/system_credentials.py','native/CredentialStore.swift')
HTTPS_CONTENT_CAPABILITIES=['codex-stdio-mcp','personal-content-explicit','https-system-credential']
CHECK_UPDATE_COMMAND='sh tools/install-mac.sh --action check-update'
UPDATE_COMMAND='sh tools/install-mac.sh --action update'
UPDATE_GUIDANCE=('For Agent Brain software updates, use the public Client repository entry: '+CHECK_UPDATE_COMMAND+' then '+UPDATE_COMMAND+'. Both reuse the verified installed release policy; main/tags are not the installed release channel. Status and repeating install only verify the local registration. The active package, running worker and latest channel release are separate states. A changed active package requires a normal new MCP worker; never claim it is loaded from the package version alone. Confirm client_runtime.running_worker_version before continuing Onboard. Record this run\'s version and actual results; prior read/timeout counts are history, not a new test.')
DEPENDENCY_RUNTIME={'dependency_policy','dependency_policy_sha256'}
HTTPS_CONTENT_RUNTIME={'https_profile','https_profile_sha256','credential_source_sha256','content_policy','content_policy_sha256','device_home','codex_home','content_cache'}
SSH_CONTENT_RUNTIME={'ssh_profile','ssh_profile_sha256','content_policy','content_policy_sha256','device_home','codex_home','content_cache'}
LEGACY_PRESERVATION={'legacy_binding_sha256','legacy_hooks_sha256'}
CONTENT_RUNTIME={'device_plan','device_plan_sha256','signing_policy','signing_policy_sha256','content_policy','content_policy_sha256','component_source_sha256','device_home','codex_home','content_cache'}


def policy_file(path, expected_sha, *, runtime_check=True):
    require_platform('release-update')
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
    policy=validate_release_policy(policy)
    require_platform("release-update",policy["platform"])
    if policy['capabilities'] in (CONTENT_CAPABILITIES,SSH_CONTENT_CAPABILITIES,HTTPS_CONTENT_CAPABILITIES):
        private_mode(path,blocked=UpdateBlocked)
    for value in [policy['repository'],policy['cache'],*(v for k,v in policy['runtime'].items() if k!='endpoint' and not k.endswith('_sha256'))]:
        if Path(value).is_symlink():raise UpdateBlocked('Linked runtime path')
    repo,cache=(Path(policy[k]).resolve() for k in ('repository','cache'))
    if repo==cache or repo in cache.parents or cache in repo.parents:raise UpdateBlocked('Separate resolved client paths required')
    if policy['capabilities'] in (CONTENT_CAPABILITIES,SSH_CONTENT_CAPABILITIES,HTTPS_CONTENT_CAPABILITIES):
        homes=[Path(policy['runtime'][k]).resolve() for k in ('device_home','codex_home','content_cache')]
        if homes[2] in homes[:2] or any(homes[2] in p.parents for p in homes[:2]) or any(p==repo or repo in p.parents or p==cache or cache in p.parents for p in homes):raise UpdateBlocked('Separate resolved native/cache paths required')
    if policy['capabilities']==HTTPS_CONTENT_CAPABILITIES:dependency_environment(policy,runtime_check=runtime_check)
    return policy


def validate_release_policy(policy):
    """Pure reviewed configuration contract; operations are authorized separately."""
    keys = {'schema', 'adapter', 'platform', 'origin', 'ref', 'anchor_commit', 'channel',
            'core_api', 'state_schema', 'capabilities', 'file_prefixes', 'repository', 'cache', 'runtime'}
    if not isinstance(policy,dict) or set(policy)!=keys or policy['schema']!=1:
        raise UpdateBlocked('Unknown client release policy')
    if policy['adapter']!='codex':raise UpdateBlocked('Reviewed Codex adapter required')
    paths=platform_paths(policy['platform'])
    if policy['core_api']!=1 or policy['state_schema']!=1 or policy['capabilities'] not in (CAPABILITIES,PERSONAL_CAPABILITIES,CONTENT_CAPABILITIES,SSH_CONTENT_CAPABILITIES,HTTPS_CONTENT_CAPABILITIES):
        raise UpdateBlocked('Client permission/protocol change requires review')
    content=policy['capabilities'] in (CONTENT_CAPABILITIES,SSH_CONTENT_CAPABILITIES,HTTPS_CONTENT_CAPABILITIES)
    ssh=policy['capabilities']==SSH_CONTENT_CAPABILITIES
    https=policy['capabilities']==HTTPS_CONTENT_CAPABILITIES
    if policy['channel']!='stable' or policy['file_prefixes']!=list(HTTPS_CONTENT_CLIENT_FILES if https else SSH_CONTENT_CLIENT_FILES if ssh else CONTENT_CLIENT_FILES if content else CLIENT_FILES):
        raise UpdateBlocked('Client release file scope changed')
    origin=policy['origin'];parsed=urlsplit(origin)
    if parsed.scheme:
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise UpdateBlocked('Credential-free HTTPS release origin required')
    elif not paths(origin).is_absolute():
        raise UpdateBlocked('Synthetic local release origin must be pinned and absolute')
    if not re.fullmatch(r'refs/heads/[a-zA-Z0-9][a-zA-Z0-9._/-]*',policy['ref']) or not re.fullmatch(r'[a-f0-9]{40}',policy['anchor_commit']):
        raise UpdateBlocked('Pinned release ref and anchor required')
    for name in ('repository','cache'):
        p=paths(policy[name])
        if not p.is_absolute() or '..' in p.parts:raise UpdateBlocked('Approved isolated client paths required')
    repo,cache=map(lambda k:paths(policy[k]),('repository','cache'))
    if repo==cache or repo in cache.parents or cache in repo.parents:
        raise UpdateBlocked('Separate client source and package cache required')
    runtime=policy['runtime']
    if content:
        https_runtime=HTTPS_CONTENT_RUNTIME-{'credential_source_sha256'} if policy['platform'] in WINDOWS_PLATFORMS else HTTPS_CONTENT_RUNTIME
        scopes=(https_runtime,https_runtime|LEGACY_PRESERVATION,https_runtime|DEPENDENCY_RUNTIME) if https else (SSH_CONTENT_RUNTIME,SSH_CONTENT_RUNTIME|LEGACY_PRESERVATION) if ssh else (CONTENT_RUNTIME,)
        if not isinstance(runtime,dict) or set(runtime) not in scopes:raise UpdateBlocked('Fixed content runtime required')
        for name,value in runtime.items():
            if name.endswith('_sha256'):
                if not isinstance(value,str) or not re.fullmatch(r'[a-f0-9]{64}',value):raise UpdateBlocked('Pinned runtime digest required')
            elif not isinstance(value,str) or not paths(value).is_absolute() or '..' in paths(value).parts:raise UpdateBlocked('Fixed runtime paths required')
        homes=[paths(runtime[k]) for k in ('device_home','codex_home','content_cache')]
        if homes[2] in homes[:2] or any(homes[2] in p.parents for p in homes[:2]):raise UpdateBlocked('Separate native/content cache required')
        if any(p==repo or repo in p.parents or p==cache or cache in p.parents for p in homes):raise UpdateBlocked('Native paths must be outside client code paths')
        return policy
    if not isinstance(runtime,dict) or set(runtime)!={'endpoint','token_file'}:
        raise UpdateBlocked('Fixed server runtime destination required')
    # The token remains server-local; validation does not read its contents.
    endpoint=urlsplit(runtime['endpoint'])
    if endpoint.scheme!='http' or endpoint.hostname!='127.0.0.1' or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in ('','/'):
        raise UpdateBlocked('Server bridge requires a fixed loopback API')
    if not paths(runtime['token_file']).is_absolute():raise UpdateBlocked('Server-local token path required')
    return policy


class ClientPackage:
    def __init__(self,policy=None):
        self.content=bool(policy and policy['capabilities'] in (CONTENT_CAPABILITIES,SSH_CONTENT_CAPABILITIES,HTTPS_CONTENT_CAPABILITIES))
        self.ssh=bool(policy and policy['capabilities']==SSH_CONTENT_CAPABILITIES)
        self.https=bool(policy and policy['capabilities']==HTTPS_CONTENT_CAPABILITIES)
        self.files=HTTPS_CONTENT_CLIENT_FILES if self.https else SSH_CONTENT_CLIENT_FILES if self.ssh else CONTENT_CLIENT_FILES if self.content else CLIENT_FILES
        self.component_source_sha=policy['runtime']['component_source_sha256'] if self.content and not self.ssh and not self.https else None

        if self.https:require_platform('credential-store',policy.get('platform'))
        self.credential_source_sha=policy['runtime']['credential_source_sha256'] if self.https else None

    def preflight(self, package, manifest):
        if set(manifest['files'])!=set(self.files) or manifest['migrations']:
            raise UpdateBlocked('Client file membership/migration change requires review')
        if self.https and manifest['files'].get('native/CredentialStore.swift')!=self.credential_source_sha:raise UpdateBlocked('Credential helper source changed; helper/profile review required')
        if self.content and not self.ssh and not self.https and manifest['files'].get('native/DeviceComponent.swift')!=self.component_source_sha:raise UpdateBlocked('Native component source changed; signed component/plan review required')
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
    def __init__(self, path, expected_sha, *, inspection=False):
        self.inspection=inspection
        self.policy=policy_file(path,expected_sha,runtime_check=not inspection)
        repo=Path(self.policy['repository'])
        if not (repo/'.git').is_dir():raise UpdateBlocked('Approved client-only release checkout required; no silent clone')
        # Bound remote discovery inside the existing 30s MCP startup budget.
        self.engine=ReleaseEngine(self.policy['cache'],self.policy,GitReleaseSource(repo,self.policy,timeout=5),ClientPackage(self.policy),{})

    def verified_active(self):
        require_platform('release-update',self.policy['platform'])
        active=self.engine.active()
        if active and self.policy['capabilities'] in (CONTENT_CAPABILITIES,SSH_CONTENT_CAPABILITIES,HTTPS_CONTENT_CAPABILITIES):
            receipt=json.loads((Path(active['package'])/'verified.json').read_text())
            self.engine.adapter.preflight(Path(active['package']),receipt['manifest'])
        return active

    def discover(self, adapter, platform, installed_version):
        if getattr(self,'inspection',False):return {'status':'release-review-required','releases':[],'next_action':'Read-only inspection cannot fetch or discover updates.'}
        try:require_platform('release-update',self.policy['platform'])
        except PlatformUnsupported as error:return error.result(releases=[])
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
            if getattr(self,'inspection',False):raise UpdateBlocked('Read-only inspection cannot update')
            require_platform('release-update',self.policy['platform'])
            result=self.engine.check()
            result['active']=self.verified_active()
            result['next_action']='Use the verified active client; native permissions and data are unchanged.'
            return result
        except PlatformUnsupported as error:return error.result(active=None)
        except (UpdateBlocked,LockTimeout,OSError,ValueError,KeyError,TypeError,subprocess.TimeoutExpired) as error:
            # Never echo upstream paths, credentials, raw stderr or exception values.
            # A lock timeout can overlap activation before its health check.
            # Do not read the live pointer; fail closed until the owner completes.
            if isinstance(error,LockTimeout):
                active=None
            else:
                try:active=self.verified_active()
                except (UpdateBlocked,LockTimeout,OSError,ValueError,KeyError,TypeError):active=None
            if isinstance(error,LockTimeout):
                error_class='LockTimeout';error_code='release-lock-busy'
            elif isinstance(error,UpdateBlocked):
                error_class='UpdateBlocked';error_code='release-policy-blocked'
            elif isinstance(error,OSError):
                error_class='OSError';error_code='release-check-failed'
            elif isinstance(error,ValueError):
                error_class='ValueError';error_code='release-check-failed'
            elif isinstance(error,KeyError):
                error_class='KeyError';error_code='release-check-failed'
            elif isinstance(error,TypeError):
                error_class='TypeError';error_code='release-check-failed'
            else:
                error_class='TimeoutExpired';error_code='release-check-failed'
            return {'status':'release-review-required','active':active,
                    'diagnostic':{'stage':'release-check','error_class':error_class,'error_code':error_code},
                    'next_action':'Inspect the pinned release policy/package or failed health check; prior activation is retained or restored.'}


def runtime_environment(python, git, core, *, windows=False, system_paths=()):
    """Pure path/environment contract; granting a platform is a separate check."""
    path_type = PureWindowsPath if windows else PurePosixPath
    separator = ';' if windows else ':'
    values = (python, git, core, *system_paths)
    if any(separator in v or any(c in v for c in ('\x00','\r','\n')) for v in values):
        raise UpdateBlocked('Unambiguous runtime paths required')
    paths = [path_type(v) for v in values]
    if any(not p.is_absolute() or '..' in p.parts for p in paths):
        raise UpdateBlocked('Absolute runtime paths required')
    return {'PATH': separator.join(str(p) for p in (paths[0].parent, paths[1].parent, *paths[3:])),
            'GIT_EXEC_PATH': str(paths[2]), 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': 'NUL' if windows else '/dev/null',
            'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_COUNT': '0'}


def dependency_environment(policy, *, runtime_check=True):
    """Pinned first-install dependencies; existing policies keep their environment."""
    runtime=policy['runtime']
    if not DEPENDENCY_RUNTIME<=set(runtime):return None
    require_platform('runtime-dependencies',policy.get('platform'))
    path=Path(runtime['dependency_policy'])
    if not path.is_absolute() or any(p.is_symlink() for p in (path,*path.parents)) or not path.is_file()  or path.stat().st_size>2097152:raise UpdateBlocked('Private dependency policy required')
    private_mode(path,owner=True,blocked=UpdateBlocked)
    raw=path.read_bytes()
    if digest(raw)!=runtime['dependency_policy_sha256']:raise UpdateBlocked('Dependencies changed')
    value=json.loads(raw)
    if value.get('schema') == 2:
        if set(value) != {'schema','python','git','git_exec_path','python_sha256','git_sha256'}:
            raise UpdateBlocked('Dependency scope changed')
        for name in ('python', 'git'):
            executable = Path(value[name])
            if not executable.is_absolute() or executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK) or digest(executable.read_bytes()) != value[name+'_sha256']:
                raise UpdateBlocked('Prepared executable changed')
        python, git, core = (Path(value[n]) for n in ('python','git','git_exec_path'))
        if not core.is_absolute() or core.is_symlink() or not (core/'git-remote-https').is_file():
            raise UpdateBlocked('Git HTTPS transport required')
        if runtime_check and Path(sys.executable).resolve() != python:
            raise UpdateBlocked('Prepared Python runtime required')
        # Current policy_file/platform validation remains Mac-only. Windows paths
        # can be tested independently, never widening the actual device grant.
        return runtime_environment(str(python), str(git), str(core), system_paths=('/usr/bin','/bin'))
    if set(value)!={'schema','root','files'} or value['schema']!=1:raise UpdateBlocked('Dependency scope changed')
    root=Path(value['root'])
    if Path(runtime['device_home']) not in root.parents or not root.is_absolute() or any(p.is_symlink() for p in (root,*root.parents)):raise UpdateBlocked('Device-local dependencies required')
    verify_tree(root,value['files'])
    python=root/'python/bin/python3';git=root/'git/bin/git';core=root/'git/libexec/git-core'
    if not python.is_file() or not git.is_file() or not core.is_dir() or not os.access(python,os.X_OK) or not os.access(git,os.X_OK):raise UpdateBlocked('Bundled Python and Git required')
    if runtime_check and Path(sys.executable).resolve()!=python.resolve():raise UpdateBlocked('Pinned Python runtime required')
    return runtime_environment(str(python), str(git), str(core), system_paths=('/usr/bin','/bin'))


def tree_files(root):
    """Inventory regular files and internal relative links, never external targets."""
    root=Path(root)
    if not root.is_dir() or root.is_symlink():raise UpdateBlocked('Canonical dependency tree required')
    files={}
    for path in sorted(root.rglob('*')):
        name=path.relative_to(root).as_posix()
        if path.is_symlink():
            target=os.readlink(path)
            if Path(target).is_absolute() or not path.resolve().is_relative_to(root.resolve()) or not path.resolve().exists():raise UpdateBlocked('External or broken dependency link')
            files[name]={'link':target}
        elif path.is_file():
            if path.stat().st_nlink!=1:raise UpdateBlocked('Linked dependency file')
            files[name]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'executable':bool(path.stat().st_mode&0o111)}
        elif not path.is_dir():raise UpdateBlocked('Unsupported dependency file')
    if not files or len(files)>20000:raise UpdateBlocked('Dependency tree size changed')
    return files


def verify_tree(root,files):
    if not isinstance(files,dict) or tree_files(root)!=files:raise UpdateBlocked('Dependency inventory changed')
