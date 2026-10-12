"""Opt-in HTTPS device content and native-only durable pairing coordinator.

Profiles contain reviewed metadata only. Codes and tokens remain inside the
dedicated OS credential item, HTTP Authorization and private helper pipes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import secrets
import sys
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

BASE=Path(__file__).resolve().parent
ROOT=BASE.parent if (BASE.parent/'scripts').is_dir() else BASE.parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from remote_http import RemoteClient,NoRedirect
from client_pairing import EnrollmentTerminal,reviewed_ids,allowed_id
from system_credentials import NativeKeychain,CredentialStore,canonical,unique,owned


def valid_secret(value):return isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{32,128}',value)
def grant_check(grant):
    keys={'actor','scopes','artifact_types','ids','adapter','platform'}
    if not isinstance(grant,dict) or set(grant)!=keys or grant['adapter']!='codex' or grant['platform'] not in ('darwin-arm64','darwin-x86_64') or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',grant.get('actor','')):raise ValueError('Frozen device grant required')
    if grant['scopes']!=['read','put'] or not isinstance(grant['artifact_types'],list) or not grant['artifact_types'] or len(set(grant['artifact_types']))!=len(grant['artifact_types']) or set(grant['artifact_types'])-{'memory','rule','skill'}:raise ValueError('Reviewed read/put grant required')
    reviewed_ids(grant['artifact_types'],grant['ids'])
    from content_contract import personal_owner
    if not personal_owner(grant['ids']) and (any(not isinstance(v,list) for v in grant['ids'].values()) or sum(map(len,grant['ids'].values()))!=31):raise ValueError('Personal owner or legacy 31 exact approved IDs required')

def endpoint_check(endpoint):
    if not isinstance(endpoint,str):raise ValueError('HTTPS origin required')
    value=urlsplit(endpoint)
    if value.scheme!='https' or not value.hostname or value.username or value.password or value.query or value.fragment or value.path not in ('','/') or any(c.isspace() for c in endpoint):raise ValueError('Credential-free HTTPS origin required')
    return endpoint.rstrip('/')

def profile_file(path,digest):
    path=owned(path)
    if path.stat().st_size>16384 or not re.fullmatch(r'[a-f0-9]{64}',digest or ''):raise ValueError('Pinned private profile required')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('Profile changed')
    value=json.loads(raw,object_pairs_hook=unique)
    keys={'schema','transport','adapter','platform','data_mode','endpoint','grant','credential_helper','credential_helper_sha256','credential_source_sha256','state_directory'}
    if not isinstance(value,dict) or set(value)!=keys or type(value['schema'])!=int or value['schema']!=1 or value['transport']!='https-system-credential' or value['adapter']!='codex' or value['platform']!='darwin-'+platform.machine().lower() or value['data_mode']!='personal':raise ValueError('HTTPS Mac content profile required')
    endpoint_check(value['endpoint']);grant_check(value['grant'])
    if value['grant']['platform']!=value['platform']:raise ValueError('Grant binding changed')
    for key in ('credential_helper','state_directory'):
        p=Path(value[key])
        if not p.is_absolute() or any(item.is_symlink() for item in (p,*p.parents)):raise ValueError('Owned credential paths required')
    for key in ('credential_helper_sha256','credential_source_sha256'):
        if not re.fullmatch(r'[a-f0-9]{64}',value[key]):raise ValueError('Credential helper pins required')
    return value

def native_store(profile,profile_sha):
    backend=NativeKeychain(profile['credential_helper'],profile['credential_helper_sha256'],'agent-brain.https.'+profile_sha)
    return CredentialStore(backend,profile['state_directory'])


class PairingHTTPS:
    def __init__(self,endpoint):
        self.endpoint=endpoint_check(endpoint)
        self.opener=build_opener(ProxyHandler({}),NoRedirect())

    def call(self,phase,payload,credential=None):
        headers={'Content-Type':'application/json'}
        if credential is not None:
            if not valid_secret(credential):raise ValueError('Invalid device credential')
            headers['Authorization']='Bearer '+credential
        request=Request(self.endpoint+'/v1/pairing/'+phase,data=canonical(payload),headers=headers,method='POST')
        try:
            with self.opener.open(request,timeout=30) as response:
                raw=response.read(16385)
                if response.status!=200:raise ValueError('Pairing unconfirmed')
            if len(raw)>16384:raise ValueError('Pairing reply exceeds limit')
            value=json.loads(raw,object_pairs_hook=unique)
            if not isinstance(value,dict) or 'error' in value:raise ValueError('Pairing unconfirmed')
            return value
        except HTTPError as error:
            # Only an explicit protocol denial is terminal; timeouts, redirects,
            # proxy failures and unknown replies preserve the same recovery.
            if phase in ('prepare','confirm') and error.code==403:
                try:
                    raw=error.read(16385)
                    value=json.loads(raw,object_pairs_hook=unique) if len(raw)<=16384 else {}
                    code=value.get('error',{}).get('code')
                except Exception:code=None
                if phase=='prepare' and code=='pairing_denied':raise EnrollmentTerminal('pairing_prepare_rejected') from None
                if phase=='confirm' and code in ('pairing_expired','device_revoked'):raise EnrollmentTerminal(code) from None
            raise ValueError('Pairing unconfirmed') from None
        except EnrollmentTerminal:raise
        except Exception:raise ValueError('Pairing unconfirmed') from None

    def prepare(self,code,nonce,profile):
        return self.call('prepare',{'code':code,'nonce':nonce,'device':{k:profile[k] for k in ('adapter','platform')}})

    def confirm(self,credential,nonce):return self.call('confirm',{'nonce':nonce},credential)


class DeviceEnrollment:
    def __init__(self,profile,profile_sha,store,transport,secure_input):
        self.profile,self.profile_sha,self.store,self.transport,self.secure_input=profile,profile_sha,store,transport,secure_input

    def check(self,saved):
        basic={'schema','profile_sha256','phase','nonce'}
        if not isinstance(saved,dict) or type(saved.get('schema'))!=int or saved['schema']!=1 or saved.get('profile_sha256')!=self.profile_sha or not valid_secret(saved.get('nonce')):raise ValueError('Enrollment record binding changed')
        if saved.get('phase')=='preparing':
            if set(saved)!=basic|{'code'} or not valid_secret(saved['code']):raise ValueError('Invalid prepare recovery')
        elif saved.get('phase') in ('pending','active'):
            if set(saved)!=basic|{'credential','device_id','grant'} or not valid_secret(saved['credential']) or not re.fullmatch(r'[a-f0-9]{32}',saved.get('device_id','')) or saved['grant']!=self.profile['grant']:raise ValueError('Invalid device recovery')
        else:raise ValueError('Unknown enrollment preserved')
        return saved

    def run(self):
        try:
            with self.store.lease():
                saved=self.store.load()
                if saved is None:
                    code=self.secure_input()
                    if code is None:return {'status':'pairing-cancelled','installed':False}
                    if not valid_secret(code):return {'status':'pairing-invalid-input','installed':False}
                    saved={'schema':1,'profile_sha256':self.profile_sha,'phase':'preparing','code':code,'nonce':secrets.token_urlsafe(24)}
                    # Persist before prepare. Lost replies/restarts reuse code+nonce.
                    self.store.save(saved,None)
                self.check(saved)
                if saved['phase']=='preparing':
                    result=self.transport.prepare(saved['code'],saved['nonce'],self.profile)
                    if set(result)!={'credential','nonce','device_id','grant','phase'} or result['phase']!='pending' or result['nonce']!=saved['nonce'] or result['grant']!=self.profile['grant']:raise ValueError('Unexpected prepare grant')
                    pending={'schema':1,'profile_sha256':self.profile_sha,**result}
                    self.check(pending);self.store.save(pending,saved);saved=pending
                result=self.transport.confirm(saved['credential'],saved['nonce'])
                if result!={'device_id':saved['device_id'],'grant':self.profile['grant'],'phase':'active'}:raise ValueError('Unexpected confirmation')
                active=dict(saved,phase='active')
                if saved!=active:self.store.save(active,saved)
                return {'status':'device-paired','device_id':saved['device_id'],'grant':self.profile['grant'],'installed':False,'next_action':'Apply the separately approved exact owned content installation plan and read-only protocol check.'}
        except EnrollmentTerminal as error:
            return {'status':'pairing-reenrollment-review-required','reason':error.reason,'installed':False}
        except Exception:
            return {'status':'pairing-resume-required','installed':False,'next_action':'Preserve the system credential record and retry the same pinned profile. No raw transport or credential details returned.'}

    def reset_terminal(self,confirm_reset):
        try:
            with self.store.lease():
                saved=self.store.load()
                if saved is None:return {'status':'pairing-not-enrolled','installed':False}
                self.check(saved)
                if saved['phase']=='preparing':
                    try:
                        # Recheck exact code+nonce, never assume a network failure
                        # means rejection. A successful prepare stays recoverable.
                        self.transport.prepare(saved['code'],saved['nonce'],self.profile)
                        return {'status':'pairing-reset-blocked','installed':False}
                    except EnrollmentTerminal as error:
                        if error.reason!='pairing_prepare_rejected':raise ValueError('Unexpected prepare terminal')
                        if not confirm_reset(error.reason,'preparing'):return {'status':'pairing-reset-cancelled','installed':False}
                        self.store.clear(saved)
                        return {'status':'pairing-reenrollment-ready','installed':False}
                try:
                    result=self.transport.confirm(saved['credential'],saved['nonce'])
                    if result!={'device_id':saved['device_id'],'grant':self.profile['grant'],'phase':'active'}:raise ValueError('Unknown confirmation')
                    return {'status':'active-enrollment-preserved','installed':False}
                except EnrollmentTerminal as error:
                    if not confirm_reset(error.reason,saved['device_id']):return {'status':'pairing-reset-cancelled','installed':False}
                    self.store.clear(saved)
                    return {'status':'pairing-reenrollment-ready','installed':False}
        except Exception:return {'status':'pairing-reset-blocked','installed':False}


class HTTPSContentClient:
    def __init__(self,path,digest,*,store=None,credential_access=True):
        self.profile,self.profile_sha=profile_file(path,digest),digest
        self.grant,self.data_mode=self.profile['grant'],'personal'
        self.store=store if store is not None else native_store(self.profile,digest)
        self.enrollment=DeviceEnrollment(self.profile,digest,self.store,None,None)
        self.device_id=None;self.queue_identity=None
        if credential_access:self.activate_identity()
        self.transport=RemoteClient(self.profile['endpoint'],data_mode='personal',credential_provider=self._credential)

    def activate_identity(self):
        saved=self.enrollment.check(self.store.load())
        if saved['phase']!='active' or self.device_id is not None and saved['device_id']!=self.device_id:raise ValueError('Confirmed unchanged device required')
        self.device_id=saved['device_id']
        self.queue_identity={'transport':'https-system-credential','endpoint':self.profile['endpoint'],'profile_sha256':self.profile_sha,'device_id':self.device_id}

    def _credential(self):
        saved=self.enrollment.check(self.store.load())
        if saved['phase']!='active' or self.device_id is None or saved['device_id']!=self.device_id:raise ValueError('Device identity changed; old outbox preserved')
        return saved['credential']

    def request(self,request):
        try:
            method=request['method'];params=request['params']
            if method not in ('content_catalog','content_read','content_put','client_updates'):raise ValueError('Device-only methods required')
            if method in ('content_read','content_put') and not allowed_id(self.grant['ids'],params['kind'],params['id']):raise ValueError('Unreviewed content ID')
            return self.transport.request(request)
        except Exception:
            return {'request_id':request.get('request_id') if isinstance(request,dict) else None,'error':{'code':'device_transport_blocked','retryable':False},'persistence':{'durable':False}}


def install_once(plan,approved_digest,*,store=None,transport=None,secure_input=None,installer=None):
    """One approved native plan: verify before UI, resume pairing, then install.

    This does not create an invitation, read a token from argv or broaden an
    existing profile. Pairing recovery remains in the same dedicated OS item.
    """
    from content_install import context,prepare,apply,private_json,InstallBlocked
    try:
        if not isinstance(plan,dict) or hashlib.sha256(canonical(plan)).hexdigest()!=approved_digest:
            raise InstallBlocked('Exact approved native plan required')
        policy,_,_,_=context(plan['device_home'],plan['package'],plan['release_policy'],plan['release_policy_sha256'])
        from client_release import HTTPS_CONTENT_CAPABILITIES
        if policy['capabilities']!=HTTPS_CONTENT_CAPABILITIES:raise InstallBlocked('HTTPS install scope required')
        runtime=policy['runtime'];profile=profile_file(runtime['https_profile'],runtime['https_profile_sha256'])
        install=installer or apply
        receipt=Path(plan['device_home'])/'.config/agent-brain-client/content-install-receipt.json'
        if receipt.exists():
            previous=private_json(receipt)
            if previous.get('phase') in ('complete','applying') and previous.get('plan_sha256')==approved_digest:
                # Existing apply verifies/restores an interrupted matching journal
                # before checking the original preimage; pairing already preceded it.
                return install(plan,approved_digest)
        if prepare(plan['device_home'],plan['package'],plan['release_policy'],plan['release_policy_sha256'],plan['replace_existing'])!=plan:
            raise InstallBlocked('Install preimage changed before credential input')
        credentials=store if store is not None else native_store(profile,runtime['https_profile_sha256'])
        enrollment=DeviceEnrollment(profile,runtime['https_profile_sha256'],credentials,transport if transport is not None else PairingHTTPS(profile['endpoint']),secure_input if secure_input is not None else credentials.backend.secure_input)
        paired=enrollment.run()
        if paired.get('status')!='device-paired':
            return {'status':paired.get('status','pairing-resume-required'),'installed':False,'native_changed':False,'credential_record_preserved':True,**({'reason':paired['reason']} if 'reason' in paired else {})}
        return install(plan,approved_digest)
    except Exception:
        return {'status':'https-install-review-or-resume-required','installed':False,'native_changed':False,'credential_record_preserved':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('inspect','pair','reset-terminal','install'))
    parser.add_argument('--https-profile');parser.add_argument('--https-profile-sha256')
    parser.add_argument('--content-install-plan');parser.add_argument('--approved-plan-sha256')
    args=parser.parse_args()
    if args.action=='install':
        try:
            from content_mcp import pinned
            result=install_once(pinned(args.content_install_plan,args.approved_plan_sha256),args.approved_plan_sha256)
        except Exception:result={'status':'https-install-review-required','installed':False,'native_changed':False}
        print(json.dumps(result));return 0 if result.get('installed') else 2
    try:
        profile=profile_file(args.https_profile,args.https_profile_sha256)
        if args.action=='inspect':result={'status':'https-content-profile-reviewed','credential_accessed':False,'installed':False,'native_changed':False,'methods':['content_catalog','content_read','content_put']}
        else:
            store=native_store(profile,args.https_profile_sha256)
            enrollment=DeviceEnrollment(profile,args.https_profile_sha256,store,PairingHTTPS(profile['endpoint']),store.backend.secure_input)
            result=enrollment.run() if args.action=='pair' else enrollment.reset_terminal(store.backend.confirm_reset)
    except Exception:result={'status':'https-device-review-required','installed':False,'native_changed':False}
    print(json.dumps(result));return 0 if result['status'] in ('device-paired','https-content-profile-reviewed') else 2

if __name__=='__main__':sys.exit(main())
