"""Opt-in Mac Codex fixed SSH proxy; no client API credential or secret discovery."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from types import SimpleNamespace
from client_pairing import reviewed_ids,allowed_id
try:
    import content_contract as contract
except ImportError:
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
    import brain_content as contract

PREFIX='/usr/bin/sudo -n -u agent-brain /usr/bin/python3 -I -B '
COMMAND=r'/opt/agent-brain-client/[A-Za-z0-9_-]+/scripts/brain_ssh_proxy\.py --policy-file /var/lib/agent-brain/auth/[A-Za-z0-9_-]+\.json --policy-sha256 [a-f0-9]{64}'

def private(path):
    path=Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path,*path.parents)) or not path.is_file() or path.stat().st_mode&0o077:raise ValueError('Private pinned file required')
    return path

def pairs(items):
    result={}
    for key,value in items:
        if key in result:raise ValueError('Duplicate metadata')
        result[key]=value
    return result

def grant_scope(grant):
    if not isinstance(grant,dict) or set(grant)!={'actor','scopes','artifact_types','ids','adapter','platform'} or grant['adapter']!='codex' or grant['platform'] not in ('darwin-arm64','darwin-x86_64') or not isinstance(grant['actor'],str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',grant['actor']):raise ValueError('Frozen grant required')
    for key,allowed in (('scopes',contract.SCOPES),('artifact_types',contract.TYPES)):
        values=grant[key]
        if not isinstance(values,list) or not values or any(not isinstance(v,str) for v in values) or len(set(values))!=len(values) or set(values)-allowed:raise ValueError('Frozen grant required')
    reviewed_ids(grant['artifact_types'],grant['ids'])

def authorize(request,grant):
    if not isinstance(request,dict) or set(request)!={'request_id','method','params'} or not isinstance(request['request_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',request['request_id']):raise ValueError('Typed request required')
    method,params=request['method'],request['params']
    if method=='client_updates':
        if 'read' not in grant['scopes'] or not isinstance(params,dict) or set(params)!={'adapter','platform','installed_version'} or params['adapter']!='codex' or params['platform']!=grant['platform'] or not isinstance(params['installed_version'],str) or not re.fullmatch(r'\d+\.\d+\.\d+',params['installed_version']):raise ValueError('Fixed read-only discovery required')
        return
    contract.validate(method,params)
    if not contract.permits_scope(grant['scopes'],method):raise ValueError('Scope denied')
    kinds=params['kinds'] if method=='content_catalog' else [params['kind']]
    if set(kinds)-set(grant['artifact_types']) or method!='content_catalog' and not allowed_id(grant['ids'],params['kind'],params['id']):raise ValueError('Scope denied')

def profile(path,digest):
    path=private(path);raw=path.read_bytes()
    if len(raw)>16384 or hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('SSH profile drift')
    value=json.loads(raw,object_pairs_hook=pairs)
    if set(value)!={'schema','adapter','platform','data_mode','transport','ssh','grant'} or value['schema']!=1 or value['adapter']!='codex' or value['transport']!='ssh-fixed-proxy' or value['data_mode']!='personal' or value['platform']!='darwin-'+platform.machine().lower() or sys.platform!='darwin':raise ValueError('Actual Mac Codex SSH profile required')
    grant_scope(value['grant'])
    if value['grant']['platform']!=value['platform']:raise ValueError('Device platform drift')
    ssh=value['ssh']
    if not isinstance(ssh,dict) or set(ssh)!={'host','user','port','identity_file','known_hosts_file','known_hosts_sha256','remote_command'} or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}',ssh['host']) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,31}',ssh['user']) or type(ssh['port'])!=int or not 1<=ssh['port']<=65535:raise ValueError('Fixed SSH destination required')
    key=private(ssh['identity_file'])  # Metadata only; OpenSSH owns the key read.
    known=private(ssh['known_hosts_file'])
    if hashlib.sha256(known.read_bytes()).hexdigest()!=ssh['known_hosts_sha256']:raise ValueError('Host identity drift')
    command=ssh['remote_command']
    if not isinstance(command,str) or not command.startswith(PREFIX) or not re.fullmatch(COMMAND,command[len(PREFIX):]):raise ValueError('Reviewed fixed proxy command required')
    args=['/usr/bin/ssh','-F','/dev/null','-T','-p',str(ssh['port']),'-i',str(key)]
    for option in ('IdentitiesOnly=yes','IdentityAgent=none','BatchMode=yes','StrictHostKeyChecking=yes','UserKnownHostsFile='+str(known),'ForwardAgent=no','ClearAllForwardings=yes','ConnectTimeout=5','ServerAliveInterval=30','ServerAliveCountMax=3'):args.extend(['-o',option])
    args.extend([ssh['user']+'@'+ssh['host'],command])
    return value,args

class SSHChannel:
    """One bounded stdio connection for this MCP process; no ControlPersist/job."""
    def __init__(self,spawn=None):
        self.spawn=spawn or subprocess.Popen;self.process=None;self.argv=None
        import atexit
        atexit.register(self.close)

    def close(self):
        process=self.process;self.process=None
        if process is None:return
        for pipe in (process.stdin,process.stdout):
            if pipe:pipe.close()
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=1)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=1)

    def __call__(self,argv,*,input,capture_output,timeout):
        import select
        try:
            if self.process is None:
                self.process=self.spawn(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0);self.argv=argv
                os.set_blocking(self.process.stdin.fileno(),False);os.set_blocking(self.process.stdout.fileno(),False)
            if self.argv!=argv or self.process.poll() is not None:raise OSError('SSH connection ended; no automatic write replay')
            deadline=time.monotonic()+timeout;remaining=memoryview(input);output=b''
            while remaining:
                wait=deadline-time.monotonic()
                if wait<=0 or not select.select([],[self.process.stdin],[],wait)[1]:raise subprocess.TimeoutExpired('/usr/bin/ssh',timeout)
                remaining=remaining[os.write(self.process.stdin.fileno(),remaining):]
            while b'\n' not in output:
                wait=deadline-time.monotonic()
                if wait<=0 or not select.select([self.process.stdout],[],[],wait)[0]:raise subprocess.TimeoutExpired('/usr/bin/ssh',timeout)
                chunk=os.read(self.process.stdout.fileno(),65536)
                if not chunk:raise OSError('SSH response unconfirmed')
                output+=chunk
                if len(output)>262144:raise ValueError('SSH response too large')
            if not output.endswith(b'\n') or output.count(b'\n')!=1:raise ValueError('Unexpected response frame')
            return SimpleNamespace(returncode=0,stdout=output)
        except Exception:self.close();raise

class SSHContentClient:
    def __init__(self,path,digest,runner=None):
        self.path,self.digest=path,digest;self.value,self.argv=profile(path,digest);self.grant=self.value['grant'];self.data_mode='personal';self.runner=runner or SSHChannel()
        self.queue_identity={'transport':'ssh-fixed-proxy','destination':{k:self.value['ssh'][k] for k in ('host','user','port')},'grant':self.grant}

    def request(self,request):
        rid=request.get('request_id') if isinstance(request,dict) else None
        def blocked(code,retry=False):return {'request_id':rid,'error':{'code':code,'retryable':retry},'persistence':{'durable':False}}
        try:
            authorize(request,self.grant)
            if profile(self.path,self.digest)!=(self.value,self.argv):return blocked('ssh_profile_review_required')
            raw=contract.encode(request)
            if len(raw)>131072:return blocked('invalid_content_request')
        except (ValueError,KeyError,TypeError,OSError):return blocked('ssh_scope_or_profile_review_required')
        try:
            run=self.runner(self.argv,input=raw+b'\n',capture_output=True,timeout=120)
            if run.returncode or len(run.stdout)>262144:return blocked('ssh_proxy_unconfirmed',True)
            lines=run.stdout.splitlines()
            if len(lines)!=1:return blocked('ssh_proxy_unconfirmed',True)
            value=json.loads(lines[0],object_pairs_hook=pairs)
            if not isinstance(value,dict) or value.get('request_id')!=rid or not isinstance(value.get('result',value.get('error')),dict):return blocked('ssh_proxy_unconfirmed',True)
            if request['method'] in contract.WRITES and 'error' not in value and value.get('persistence',{}).get('durable') is not True:return blocked('ssh_proxy_unconfirmed',True)
            if request['method']=='content_catalog' and 'error' not in value:
                items=value['result']['artifacts']
                if not isinstance(items,list) or any(not isinstance(i,dict) or i.get('kind') not in request['params']['kinds'] or not isinstance(i.get('id'),str) for i in items):return blocked('ssh_proxy_unconfirmed',True)
                value['result']['artifacts']=[i for i in items if allowed_id(self.grant['ids'],i['kind'],i['id'])]
            return value
        except (ValueError,KeyError,TypeError,OSError,subprocess.TimeoutExpired):return blocked('ssh_proxy_unconfirmed',True)

    def discover(self,installed_version):
        import uuid
        # Discovery metadata cannot change the already pinned client source.
        return self.request({'request_id':uuid.uuid4().hex,'method':'client_updates','params':{'adapter':'codex','platform':self.grant['platform'],'installed_version':installed_version}})
