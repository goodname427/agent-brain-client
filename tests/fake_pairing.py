"""Test-only enrollment peer, not a server implementation or signing authority."""
import copy,hashlib,secrets,time
from contextlib import contextmanager
class PairingBlocked(Exception):
    def __init__(self,code):self.code=code
class PairingAuthority:
    def __init__(self,path,key):self.clock=time.time;self.rows={};self.by_token={}
    def issue(self,grant,ttl=300):
        code=secrets.token_urlsafe(32);self.rows[code]={'grant':copy.deepcopy(grant),'phase':'issued','expires':self.clock()+ttl};return code
    def prepare(self,code,nonce,device,**kw):
        row=self.rows[code]
        if row['phase']=='issued':
            if self.clock()>row['expires']:raise PairingBlocked('pairing_expired')
            row.update(phase='pending',nonce=nonce,device_id=secrets.token_hex(16),credential=secrets.token_urlsafe(32));self.by_token[row['credential']]=row
        if row['nonce']!=nonce:raise PairingBlocked('pairing_denied')
        return {k:copy.deepcopy(row[k]) for k in ['device_id','credential','nonce','grant','phase']}
    def confirm(self,token,nonce,**kw):
        row=self.by_token[token]
        if row['phase']=='revoked':raise PairingBlocked('device_revoked')
        if row['phase']=='pending' and self.clock()>row['expires']:raise PairingBlocked('pairing_expired')
        if row['nonce']!=nonce:raise PairingBlocked('pairing_denied')
        row['phase']='active';return {'device_id':row['device_id'],'phase':'active','grant':copy.deepcopy(row['grant'])}
    def revoke(self,device):
        for row in self.rows.values():
            if row.get('device_id')==device:row['phase']='revoked'
    @contextmanager
    def db(self):
        authority=self
        class Cursor:
            def execute(self,*args):return self
            def fetchone(self):return [next(iter(authority.rows.values()))['phase']]
        yield Cursor()
