"""Enrollment orchestration inside a trusted device component, not an Agent tool.

Interfaces allow existing HTTPS/SSH and future approved tunnel transports. No
transport or credential store is created implicitly; all secrets stay internal.
"""
import secrets

def reviewed_ids(types,ids):
    import re
    if not isinstance(ids,dict) or set(ids)!=set(types):raise ValueError('Reviewed content IDs required')
    for kind,values in ids.items():
        if kind=='memory' and values=='*':continue
        if not isinstance(values,list) or len(values)>256 or any(not isinstance(v,str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',v) for v in values) or len(set(values))!=len(values):raise ValueError('Reviewed content IDs required')

def allowed_id(ids,kind,identity):return kind=='memory' and ids.get(kind)=='*' or identity in ids.get(kind,[])

class EnrollmentTerminal(Exception):
    def __init__(self,reason):
        if reason not in ('pairing_expired','device_revoked'):raise ValueError('Not a terminal enrollment')
        self.reason=reason;super().__init__(reason)


class Enrollment:
    def __init__(self,transport,store,secure_input,profile):
        self.transport,self.store,self.secure_input,self.profile=transport,store,secure_input,profile

    def run(self):
        # The store must atomically persist credential + nonce before confirm.
        # On restart, reuse it instead of asking for the code a second time.
        try:
            saved=self.store.load()
            if saved is None:
                code=self.secure_input()  # Native secure UI; never stdin/argv/chat.
                if code is None:return {'status':'pairing-cancelled','installed':False}
                prepared=self.transport.prepare(code,secrets.token_urlsafe(24),self.profile)
                saved={k:prepared[k] for k in ('credential','nonce','device_id')}
                self.store.save(saved)
            confirmed=self.transport.confirm(saved['credential'],saved['nonce'])
            if confirmed['phase']!='active' or confirmed['device_id']!=saved['device_id']:raise ValueError('Invalid device confirmation')
            return {'status':'device-paired','device_id':confirmed['device_id'],'grant':confirmed['grant'],
                    'installed':False,'next_action':'Run the owned native installation plan and protocol check; pairing alone is not installation.'}
        except EnrollmentTerminal as error:
            return {'status':'pairing-reenrollment-review-required','reason':error.reason,'installed':False,
                    'next_action':'Use the trusted component UI to confirm clearing only this exact terminal record, then obtain a fresh code; preserve active or unknown entries.'}
        except Exception:
            return {'status':'pairing-resume-required','installed':False,
                    'next_action':'Preserve private component state and resume. No credential or raw transport error is returned.'}

    def reset_terminal(self,confirm_reset):
        try:
            saved=self.store.load()
            if saved is None:return {'status':'pairing-not-enrolled','installed':False}
            try:
                result=self.transport.confirm(saved['credential'],saved['nonce'])
                if result.get('phase')!='active' or result.get('device_id')!=saved['device_id']:raise ValueError('Unknown confirmation')
                return {'status':'active-enrollment-preserved','installed':False}
            except EnrollmentTerminal as error:
                if not confirm_reset(error.reason,saved['device_id']):return {'status':'pairing-reset-cancelled','installed':False}
                # Store clears only the exact observed record; a changed/new
                # registration must survive. Native uses its persistent ref.
                self.store.clear(saved)
                return {'status':'pairing-reenrollment-ready','installed':False}
        except Exception:return {'status':'pairing-reset-blocked','installed':False}


def inspect_plan(path):
    """Read only non-secret planned component/profile metadata, never Keychain."""
    import hashlib
    import json
    from pathlib import Path
    import platform
    import sys
    try:
        plan=Path(path)
        if plan.is_symlink() or not plan.is_file() or plan.stat().st_mode&0o077 or plan.stat().st_size>16384:raise ValueError('Private plan required')
        def unique(items):
            value={}
            for key,item in items:
                if key in value:raise ValueError('Duplicate plan/profile field')
                value[key]=item
            return value
        value=json.loads(plan.read_text(),object_pairs_hook=unique)
        if set(value)!={'schema','adapter','platform','profile','profile_sha256','component','component_sha256'} or value['schema']!=1 or value['adapter']!='codex':raise ValueError('Unknown plan')
        if sys.platform!='darwin' or value['platform']!='darwin-'+platform.machine().lower():return {'status':'adapter-review-required','native_changed':False}
        profile=Path(value['profile']);component=Path(value['component'])
        if not profile.is_absolute() or not component.is_absolute() or profile.is_symlink() or component.is_symlink():raise ValueError('Owned absolute files required')
        if not profile.is_file() or profile.stat().st_mode&0o077 or profile.stat().st_size>16384:return {'status':'pairing-profile-review-required','native_changed':False}
        raw=profile.read_bytes()
        if len(raw)>16384 or hashlib.sha256(raw).hexdigest()!=value['profile_sha256']:raise ValueError('Profile drift')
        metadata=json.loads(raw,object_pairs_hook=unique)
        from urllib.parse import urlsplit
        keys={'schema','transport','endpoint','adapter','platform','data_mode','access_group','grant'}
        if not isinstance(metadata,dict) or set(metadata) not in (keys,keys|{'issuer_policy_sha256'}) or metadata['schema']!=1 or metadata['transport']!='https' or metadata['adapter']!='codex' or metadata['platform']!=value['platform'] or metadata['data_mode'] not in ('synthetic','personal'):raise ValueError('Profile mismatch')
        if 'issuer_policy_sha256' in metadata:
            import re
            if not isinstance(metadata['issuer_policy_sha256'],str) or not re.fullmatch(r'[a-f0-9]{64}',metadata['issuer_policy_sha256']):raise ValueError('Fixed issuer policy digest required')
        endpoint=urlsplit(metadata['endpoint'])
        if endpoint.scheme!='https' or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in ('','/'):raise ValueError('Unapproved destination')
        if not isinstance(metadata['access_group'],str) or not metadata['access_group'] or not isinstance(metadata['grant'],dict):raise ValueError('Missing reviewed grant')
        # No launch, signing check, Keychain query, transport or configuration here.
        if not component.is_file():return {'status':'device-component-install-review-required','native_changed':False}
        if hashlib.sha256(component.read_bytes()).hexdigest()!=value['component_sha256']:raise ValueError('Component drift')
        return {'status':'pairing-ready-for-secure-user-input','native_changed':False,'installed':False,
                'next_action':'Use the approved signed device component secure UI. Actual Keychain access, transport and device grants require their deployment authorization; then verify native installation.'}
    except Exception:
        return {'status':'pairing-plan-review-required','native_changed':False,'installed':False,
                'next_action':'Preserve the plan and report its component/profile/platform mismatch; do not read credentials or bypass permissions.'}


class ComponentClient:
    """Reviewed signed component proxy. Model-side code never reads Keychain.

    Signing authorization is supplied explicitly, not inferred from a plan.
    The injectable runner is for synthetic tests; default runs only fixed argv.
    """
    def __init__(self,plan_path,plan_sha256,signing,runner=None):
        import hashlib,json,re,subprocess
        from pathlib import Path
        self.path=Path(plan_path)
        if inspect_plan(self.path)['status']!='pairing-ready-for-secure-user-input' or hashlib.sha256(self.path.read_bytes()).hexdigest()!=plan_sha256:raise ValueError('Reviewed component plan required')
        if not isinstance(signing,dict) or set(signing)!={'identifier','team_id'} or not isinstance(signing['identifier'],str) or not re.fullmatch(r'[A-Za-z0-9]+(?:\.[A-Za-z0-9_-]+)+',signing['identifier']) or not isinstance(signing['team_id'],str) or not re.fullmatch(r'[A-Z0-9]{10}',signing['team_id']):raise ValueError('Reviewed signing identity required')
        self.plan=json.loads(self.path.read_text());self.plan_sha=plan_sha256;self.runner=runner or subprocess.run
        profile=json.loads(Path(self.plan['profile']).read_text())
        if not profile['access_group'].startswith(signing['team_id']+'.'):raise ValueError('Reviewed access group required')
        grant=profile['grant']
        if set(grant)!={'actor','scopes','artifact_types','ids','adapter','platform'} or grant['adapter']!='codex' or grant['platform']!=self.plan['platform'] or not isinstance(grant['actor'],str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}',grant['actor']):raise ValueError('Reviewed device grant required')
        for field,allowed in (('scopes',{'read','write'}),('artifact_types',{'memory','rule','skill'})):
            values=grant[field]
            if not isinstance(values,list) or not values or any(not isinstance(v,str) for v in values) or len(set(values))!=len(values) or set(values)-allowed:raise ValueError('Reviewed device grant required')
        reviewed_ids(grant['artifact_types'],grant['ids']);self.grant=grant;self.data_mode=profile['data_mode']
        self.requirement='anchor apple generic and identifier "'+signing['identifier']+'" and certificate leaf[subject.OU] = "'+signing['team_id']+'"'

    def request(self,request):
        import hashlib,json,re
        rid=request.get('request_id') if isinstance(request,dict) else None
        failure={'request_id':rid,'error':{'code':'device_component_unconfirmed','retryable':True},'persistence':{'durable':False}}
        def blocked(code):return {'request_id':rid,'error':{'code':code,'retryable':False},'persistence':{'durable':False}}
        try:
            if not isinstance(rid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}',rid) or set(request)!={'request_id','method','params'}:return blocked('invalid_content_request')
            name,params=request['method'],request['params']
            if name not in ('content_read','content_catalog','content_put','content_remove') or not isinstance(params,dict):return blocked('invalid_content_request')
            if name=='content_remove':return blocked('device_scope_review_required')
            if ('write' if name in ('content_put','content_remove') else 'read') not in self.grant['scopes']:return blocked('device_scope_review_required')
            kinds=params.get('kinds') if name=='content_catalog' else [params.get('kind')]
            if not isinstance(kinds,list) or not kinds or any(k not in self.grant['artifact_types'] for k in kinds):return blocked('device_scope_review_required')
            if name!='content_catalog' and (not isinstance(params.get('id'),str) or not allowed_id(self.grant['ids'],params['kind'],params['id'])):return blocked('device_scope_review_required')
            if hashlib.sha256(self.path.read_bytes()).hexdigest()!=self.plan_sha or inspect_plan(self.path)['status']!='pairing-ready-for-secure-user-input':return blocked('device_component_review_required')
            from pathlib import Path
            if Path(self.plan['component']).stat().st_mode&0o022:return blocked('device_component_review_required')
            # Fail on signing/entitlement blockers. No ad-hoc fallback or prompts
            # to loosen native permission are generated by this adapter.
            verified=self.runner(['/usr/bin/codesign','--verify','--strict','-R',self.requirement,self.plan['component']],capture_output=True,timeout=5)
            if verified.returncode:return blocked('device_signature_review_required')
            payload=json.dumps(request,ensure_ascii=False).encode()
            if len(payload)>131072:return failure
            result=self.runner([self.plan['component'],'--request','--profile',self.plan['profile'],'--profile-sha256',self.plan['profile_sha256']],input=payload,capture_output=True,timeout=20)
            if result.returncode or len(result.stdout)>262144:return failure
            response=json.loads(result.stdout)
            if not isinstance(response,dict) or response.get('request_id')!=rid or not isinstance(response.get('result',response.get('error')),dict):return failure
            if request.get('method') in ('content_put','content_remove') and 'error' not in response and response.get('persistence',{}).get('durable') is not True:return failure
            if name=='content_catalog' and 'error' not in response:
                artifacts=response['result'].get('artifacts')
                if not isinstance(artifacts,list) or any(not isinstance(v,dict) or v.get('kind') not in kinds or not isinstance(v.get('id'),str) for v in artifacts):return failure
                response['result']['artifacts']=[v for v in artifacts if allowed_id(self.grant['ids'],v['kind'],v['id'])]
            return response
        except Exception:return failure
