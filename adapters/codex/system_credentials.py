"""Pinned native login Keychain bridge. No plaintext fallback or secret CLI args.

The helper is a separately reviewed executable, not a generic interpreter. Its
private pipe replies are consumed here. This protects normal Agent workflows;
it does not claim isolation from malicious software running as the same user.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from brain_lock import locked, private_mode, require_platform, platform_paths, WINDOWS_PLATFORMS


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def unique(items):
    result={}
    for key,value in items:
        if key in result:raise ValueError('Duplicate credential metadata')
        result[key]=value
    return result

def owned(path, *, directory=False):
    path=Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path,*path.parents)):raise ValueError('Owned canonical path required')
    return private_mode(path,directory=directory,owner=True)


class NativeKeychain:
    def __init__(self,helper,digest,service,*,runner=subprocess.run):
        self.helper,self.digest,self.service,self.runner=Path(helper),digest,service,runner
        if not re.fullmatch(r'agent-brain\.https\.[a-f0-9]{64}',service):raise ValueError('Dedicated credential item required')

    def call(self,op,**fields):
        require_platform('credential-store')
        helper=owned(self.helper)
        if not re.fullmatch(r'[a-f0-9]{64}',self.digest) or helper.stat().st_size>16777216 or hashlib.sha256(helper.read_bytes()).hexdigest()!=self.digest:raise ValueError('Credential helper review required')
        if not os.access(helper,os.X_OK):raise ValueError('Credential helper must be executable')
        for parent in helper.parents:
            if parent.stat().st_mode&0o022:raise ValueError('Credential helper ancestor writable')
        # No ambient injection, proxy, HOME override, debug flags or secret argv.
        env={'PATH':'/usr/bin:/bin','HOME':str(Path.home()),'LANG':'en_US.UTF-8'}
        result=self.runner([str(helper)],input=canonical(dict(op=op,service=self.service,**fields)),capture_output=True,timeout=180,env=env)
        if result.returncode or len(result.stdout)>32768:raise ValueError('Credential store unavailable')
        value=json.loads(result.stdout,object_pairs_hook=unique)
        if not isinstance(value,dict) or 'error' in value:raise ValueError('Credential store unavailable')
        return value

    def load(self):
        value=self.call('load')
        if set(value)!={'record'} or value['record'] is not None and not isinstance(value['record'],dict):raise ValueError('Unknown credential record')
        return value['record']

    def save(self,record,expected):
        if self.call('save',record=record,expected=expected)!={'saved':True}:raise ValueError('Credential save unconfirmed')

    def clear(self,expected):
        if self.call('clear',expected=expected)!={'saved':True}:raise ValueError('Credential clear unconfirmed')

    def secure_input(self):
        value=self.call('input')
        if value=={'cancelled':True}:return None
        if set(value)!={'code'} or not isinstance(value['code'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',value['code']):raise ValueError('Invalid secure input')
        return value['code']

    def confirm_reset(self,reason,device_id):
        if reason=='pairing_prepare_rejected' and device_id=='preparing':
            # Non-secret, constant OS dialog: keep the existing compiled helper
            # and its source pin compatible with installed update policies.
            require_platform('credential-store')
            script='display dialog "The server explicitly rejected this invitation. Clear only its saved preparing record? Pending or active device credentials and offline writes are preserved." with title "Agent Brain pairing" buttons {"Cancel", "Clear rejected invitation"} default button "Cancel" cancel button "Cancel"'
            result=self.runner(['/usr/bin/osascript','-e',script],capture_output=True,timeout=180,env={'PATH':'/usr/bin:/bin','HOME':str(Path.home()),'LANG':'en_US.UTF-8'})
            return result.returncode==0 and result.stdout.strip()==b'button returned:Clear rejected invitation'
        value=self.call('confirm-reset',reason=reason,device_id=device_id)
        if set(value)!={'confirmed'} or type(value['confirmed'])!=bool:raise ValueError('Reset UI unconfirmed')
        return value['confirmed']


class CredentialStore:
    def __init__(self,backend,state_directory):
        self.backend,self.directory=backend,Path(state_directory)

    def lease(self):
        # Only private non-secret locks live on disk. The entire enrollment
        # holds one lease, so concurrent starts cannot replace each other's item.
        require_platform('credential-store')
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        owned(self.directory,directory=True)
        path=self.directory/'pairing.lock'
        try:
            fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        except FileExistsError:pass
        owned(path)
        return locked(path,timeout=0)

    def load(self):return self.backend.load()
    def save(self,record,expected):self.backend.save(record,expected)
    def clear(self,expected):self.backend.clear(expected)


def credential_metadata(target, descriptor):
    """Validate nonsecret backend metadata separately from choosing a real store."""
    paths = platform_paths(target)
    if target in WINDOWS_PLATFORMS:
        if descriptor != {'kind': 'unsupported', 'platform': target}:
            raise ValueError('Unimplemented platform backend must be explicit')
        return descriptor
    keys = {'kind', 'helper', 'helper_sha256', 'source_sha256'}
    if not isinstance(descriptor, dict) or set(descriptor) != keys or descriptor['kind'] != 'mac-login-keychain':
        raise ValueError('Reviewed native credential metadata required')
    helper = paths(descriptor['helper'])
    if not helper.is_absolute() or '..' in helper.parts:
        raise ValueError('Absolute credential helper required')
    if any(not re.fullmatch(r'[a-f0-9]{64}', descriptor[k]) for k in ('helper_sha256','source_sha256')):
        raise ValueError('Credential pins required')
    return descriptor


def credential_fields(target, descriptor):
    credential_metadata(target, descriptor)
    if target in WINDOWS_PLATFORMS:
        return {'schema': 2, 'credential_backend': descriptor}
    return {'schema': 1, 'credential_helper': descriptor['helper'],
            'credential_helper_sha256': descriptor['helper_sha256'],
            'credential_source_sha256': descriptor['source_sha256']}


def profile_credentials(value):
    target = value['platform']
    platform_paths(target)
    if type(value.get('schema')) != int:
        raise ValueError('Credential metadata schema required')
    if target in WINDOWS_PLATFORMS:
        if value['schema'] != 2:raise ValueError('Explicit credential backend required')
        descriptor = value['credential_backend']
    else:
        if value['schema'] != 1:raise ValueError('Legacy native credential metadata required')
        descriptor = {'kind': 'mac-login-keychain', 'helper': value['credential_helper'],
                      'helper_sha256': value['credential_helper_sha256'],
                      'source_sha256': value['credential_source_sha256']}
    return credential_metadata(target, descriptor)


def credential_paths(profile):
    require_platform('credential-store', profile['platform'])
    for key in ('credential_helper', 'state_directory'):
        p = Path(profile[key])
        if any(item.is_symlink() for item in (p, *p.parents)):
            raise ValueError('Canonical credential runtime required')


def store_for_profile(profile, profile_sha):
    require_platform('credential-store', profile.get('platform'))
    backend = NativeKeychain(profile['credential_helper'], profile['credential_helper_sha256'],
                             'agent-brain.https.' + profile_sha)
    return CredentialStore(backend, profile['state_directory'])
