"""HTTPS content with fake Keychain/UI, synthetic ledger and isolated homes."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'));sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'adapters/codex'))
from fake_pairing import PairingAuthority
from https_content import DeviceEnrollment,HTTPSContentClient,PairingHTTPS,profile_file,install_once
from system_credentials import CredentialStore,NativeKeychain,canonical
from content_cache import ContentAdapter,CacheBlocked
from remote_http import RemoteClient,NoRedirect
from build_client import export
from client_release import HTTPS_CONTENT_CLIENT_FILES,HTTPS_CONTENT_CAPABILITIES

ROOT=Path(__file__).resolve().parents[1]
GRANT={'actor':'owner-adapter','scopes':['read','put'],'artifact_types':['memory','rule','skill'],
       'ids':{'memory':['synthetic-memory']+['synthetic-memory-'+str(i) for i in range(28)],'rule':['synthetic-rule'],'skill':['synthetic-skill']},'adapter':'codex','platform':'darwin-'+platform.machine().lower()}

class Backend:
    def __init__(self):self.record=None;self.fail=False
    def load(self):return copy.deepcopy(self.record)
    def save(self,record,expected):
        if self.record!=expected or self.fail:raise ValueError('Synthetic CAS/save blocked')
        self.record=copy.deepcopy(record)
    def clear(self,expected):
        if self.record!=expected:raise ValueError('Synthetic changed record preserved')
        self.record=None

class Transport:
    def __init__(self,authority):self.authority=authority;self.lost_prepare=False;self.lost_confirm=False;self.prepares=[]
    def prepare(self,code,nonce,profile):
        self.prepares.append((code,nonce));value=self.authority.prepare(code,nonce,{k:profile[k] for k in ('adapter','platform')})
        if self.lost_prepare:self.lost_prepare=False;raise OSError('Synthetic lost reply')
        return value
    def confirm(self,token,nonce):
        from fake_pairing import PairingBlocked
        from client_pairing import EnrollmentTerminal
        try:value=self.authority.confirm(token,nonce)
        except PairingBlocked as error:
            if error.code in ('device_revoked','pairing_expired'):raise EnrollmentTerminal(error.code)
            raise
        if self.lost_confirm:self.lost_confirm=False;raise OSError('Synthetic lost confirmation')
        return value

class HTTPSContent(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name).resolve();self.home=self.base/'home';self.home.mkdir(mode=0o700)
        self.environment=patch.dict(os.environ,HOME=str(self.home),CODEX_HOME=str(self.home/'codex'),clear=False);self.environment.start();self.addCleanup(self.environment.stop)
        self.backend=Backend();self.store=CredentialStore(self.backend,self.home/'.config/agent-brain-client/https')
        self.authority=PairingAuthority(self.base/'synthetic.sqlite3',b'SYNTHETIC_HTTPS_AUTHORITY_KEY_1234567890');self.transport=Transport(self.authority)
        self.code=self.authority.issue(GRANT,300);self.prompts=0
        self.profile={'schema':1,'transport':'https-system-credential','adapter':'codex','platform':GRANT['platform'],'data_mode':'personal','endpoint':'https://synthetic.invalid','grant':copy.deepcopy(GRANT),'credential_helper':str(self.home/'helper'),'credential_helper_sha256':'a'*64,'credential_source_sha256':hashlib.sha256((ROOT/'native/CredentialStore.swift').read_bytes()).hexdigest(),'state_directory':str(self.store.directory)}
        self.profile_path=self.base/'profile.json';self.profile_path.write_bytes(canonical(self.profile));self.profile_path.chmod(0o600);self.digest=hashlib.sha256(self.profile_path.read_bytes()).hexdigest()
    def input(self):self.prompts+=1;return self.code
    def coordinator(self):return DeviceEnrollment(self.profile,self.digest,self.store,self.transport,self.input)
    def paired(self):
        self.assertEqual(self.coordinator().run()['status'],'device-paired');return HTTPSContentClient(self.profile_path,self.digest,store=self.store)

    def test_lost_prepare_and_restart_reuse_private_code_nonce_then_remove_code(self):
        self.transport.lost_prepare=True
        first=self.coordinator().run();self.assertEqual(first['status'],'pairing-resume-required');self.assertEqual(self.backend.record['phase'],'preparing')
        saved=copy.deepcopy(self.backend.record);second=self.coordinator().run();self.assertEqual(second['status'],'device-paired');self.assertEqual(self.prompts,1)
        self.assertEqual(self.transport.prepares,[(saved['code'],saved['nonce'])]*2);self.assertEqual(self.backend.record['phase'],'active');self.assertNotIn('code',self.backend.record)
        disk=b''.join(p.read_bytes() for p in self.home.rglob('*') if p.is_file());self.assertNotIn(self.code.encode(),disk);self.assertNotIn(self.backend.record['credential'].encode(),disk)
        for public in (first,second):
            self.assertNotIn(self.code,json.dumps(public));self.assertNotIn(self.backend.record['credential'],json.dumps(public));self.assertNotIn(saved['nonce'],json.dumps(public))

    def test_lost_confirm_recovers_after_invite_expiry_without_second_prompt(self):
        self.transport.lost_confirm=True;self.assertEqual(self.coordinator().run()['status'],'pairing-resume-required');self.assertEqual(self.backend.record['phase'],'pending')
        self.authority.clock=lambda:10**12
        self.assertEqual(self.coordinator().run()['status'],'device-paired');self.assertEqual(self.prompts,1);self.assertEqual(self.backend.record['phase'],'active')

    def test_save_failure_before_network_cancellation_and_unknown_record_preserved(self):
        self.backend.fail=True;self.assertEqual(self.coordinator().run()['status'],'pairing-resume-required');self.assertFalse(self.transport.prepares);self.assertIsNone(self.backend.record)
        self.backend.fail=False
        self.assertEqual(DeviceEnrollment(self.profile,self.digest,self.store,self.transport,lambda:None).run()['status'],'pairing-cancelled');self.assertIsNone(self.backend.record)
        self.backend.record={'unrecognized':'SYNTHETIC_UNKNOWN'};before=copy.deepcopy(self.backend.record)
        self.assertEqual(self.coordinator().run()['status'],'pairing-resume-required');self.assertEqual(self.backend.record,before)

    def test_exact_grant_mismatch_does_not_confirm_or_discard_prepare_recovery(self):
        original=self.transport.prepare
        def mismatch(*args):
            value=original(*args);value['grant']=dict(value['grant'],scopes=['read','put','remove']);return value
        self.transport.prepare=mismatch;self.assertEqual(self.coordinator().run()['status'],'pairing-resume-required');self.assertEqual(self.backend.record['phase'],'preparing')
        with self.authority.db() as db:self.assertEqual(db.execute('SELECT phase FROM enrollments').fetchone()[0],'pending')

    def test_terminal_reset_only_exact_revoked_record_and_active_preserved(self):
        self.paired();before=copy.deepcopy(self.backend.record)
        self.assertEqual(self.coordinator().reset_terminal(lambda *args:True)['status'],'active-enrollment-preserved');self.assertEqual(self.backend.record,before)
        self.authority.revoke(before['device_id']);self.assertEqual(self.coordinator().run()['reason'],'device_revoked')
        self.assertEqual(self.coordinator().reset_terminal(lambda *args:False)['status'],'pairing-reset-cancelled');self.assertEqual(self.backend.record,before)
        def changed(*args):self.backend.record=dict(before,device_id='f'*32);return True
        self.assertEqual(self.coordinator().reset_terminal(changed)['status'],'pairing-reset-blocked');self.assertEqual(self.backend.record['device_id'],'f'*32)

    def test_concurrent_pairing_lease_blocks_before_ui_and_credential_reads(self):
        with self.store.lease():
            self.assertEqual(self.coordinator().run()['status'],'pairing-resume-required');self.assertEqual(self.prompts,0)

    def test_profile_inspection_never_reads_store_and_rejects_wildcard_scope_http(self):
        client=HTTPSContentClient(self.profile_path,self.digest,store=object(),credential_access=False);self.assertEqual(client.grant,GRANT);self.assertIsNone(client.queue_identity)
        for mutate in (lambda p:p.update(endpoint='http://127.0.0.1:8765'),lambda p:p['grant']['ids'].update(memory='*'),lambda p:p['grant'].update(scopes=['read','put','remove'])):
            value=copy.deepcopy(self.profile);mutate(value);self.profile_path.write_bytes(canonical(value));digest=hashlib.sha256(self.profile_path.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):profile_file(self.profile_path,digest)

    def test_content_https_headers_internal_credential_and_no_remove(self):
        client=self.paired();seen=[]
        class Response:
            def __enter__(s):return s
            def __exit__(s,*args):pass
            def read(s,*args):return json.dumps({'request_id':'synthetic_request_123456','result':{},'persistence':{'state':'read-only'}}).encode()
        class Opener:
            def open(s,request,**kwargs):seen.append(request);return Response()
        client.transport.opener=Opener();request={'request_id':'synthetic_request_123456','method':'content_read','params':{'kind':'memory','id':'synthetic-memory'}}
        reply=client.request(request);self.assertNotIn('error',reply)
        self.assertEqual(seen[0].get_header('Authorization'),'Bearer '+self.backend.record['credential']);self.assertEqual(seen[0].full_url,'https://synthetic.invalid/v1/requests');self.assertNotIn(self.backend.record['credential'].encode(),seen[0].data)
        self.assertEqual(client.request(dict(request,method='content_remove'))['error']['code'],'device_transport_blocked');self.assertEqual(len(seen),1)
        self.backend.record['device_id']='f'*32;self.assertEqual(client.request(request)['error']['code'],'device_transport_blocked');self.assertEqual(len(seen),1)

    def test_device_change_retains_old_outbox_in_distinct_namespace(self):
        first=self.paired();policy={'schema':1,'adapter':'codex','kinds':GRANT['artifact_types'],'ids':GRANT['ids'],'dependencies':{'commands':[],'python_modules':[]}}
        cache=self.base/'cache';adapter=ContentAdapter(self.home,cache,policy,first,codex_home=self.home/'codex');old=adapter.queue_sha
        request={'request_id':'synthetic_pending_write_123456','method':'content_put','params':{'kind':'memory','id':'synthetic-memory','record':{'title':'Synthetic','description':'Synthetic only','body':'Synthetic offline','type':'feedback'},'expected_revision':'missing'}}
        adapter._enqueue(request);outbox=cache/'outbox'/ (request['request_id']+'.json');before=outbox.read_bytes()
        self.backend.record['device_id']='f'*32
        second=HTTPSContentClient(self.profile_path,self.digest,store=self.store);other=ContentAdapter(self.home,cache,policy,second,codex_home=self.home/'codex');self.assertNotEqual(old,other.queue_sha)
        self.assertNotIn(self.backend.record['credential'],json.dumps(first.queue_identity));self.assertEqual(first.request({'request_id':'synthetic_request_123456','method':'content_catalog','params':{'kinds':['memory']}})['error']['code'],'device_transport_blocked')
        with self.assertRaises(CacheBlocked):other._drain()
        self.assertEqual(outbox.read_bytes(),before)

    def test_https_owned_install_plan_readonly_then_failed_postcheck_restores_preimage(self):
        import content_install as install
        import client_release as releases
        native=self.home/'codex';native.mkdir(mode=0o700);config=native/'config.toml';config.write_text('model="synthetic"\n');config.chmod(0o600);before=config.read_bytes()
        package=self.base/'package';manifest=export(ROOT,package,https_content_candidate=True)
        content=self.base/'content.json';content.write_bytes(canonical({'schema':1,'adapter':'codex','kinds':GRANT['artifact_types'],'ids':GRANT['ids'],'dependencies':{'commands':[],'python_modules':[]}}));content.chmod(0o600)
        runtime={'https_profile':str(self.profile_path),'https_profile_sha256':self.digest,'credential_source_sha256':self.profile['credential_source_sha256'],'content_policy':str(content),'content_policy_sha256':hashlib.sha256(content.read_bytes()).hexdigest(),'device_home':str(self.home),'codex_home':str(native),'content_cache':str(self.base/'content-cache')}
        policy={'schema':1,'adapter':'codex','platform':GRANT['platform'],'origin':'https://synthetic.invalid/client.git','ref':'refs/heads/releases/stable','anchor_commit':'a'*40,'channel':'stable','core_api':1,'state_schema':1,'capabilities':HTTPS_CONTENT_CAPABILITIES,'file_prefixes':list(HTTPS_CONTENT_CLIENT_FILES),'repository':str(self.base/'repository'),'cache':str(self.base/'release-cache'),'runtime':runtime}
        path=self.base/'release-policy.json';path.write_bytes(canonical(policy));path.chmod(0o600);digest=hashlib.sha256(path.read_bytes()).hexdigest()
        with patch.object(NativeKeychain,'call',side_effect=AssertionError('No real credential access during plan')):
            plan=install.prepare(self.home,package,path,digest);self.assertEqual(config.read_bytes(),before);self.assertEqual(plan['mcp']['enabled_tools'],['content_catalog','content_put','content_read'])
            calls=[]
            def check(value):
                calls.append(value)
                if len(calls)==2:raise ValueError('Synthetic postcheck failure')
            result=install.apply(plan,install.sha(install.contract.encode(plan)),check)
            self.assertEqual(result['status'],'content-install-rolled-back');self.assertEqual(config.read_bytes(),before);self.assertFalse((self.home/'.config/agent-brain-client/codex-mcp.json').exists())
        bad=copy.deepcopy(policy);bad['capabilities']=releases.SSH_CONTENT_CAPABILITIES;path.write_bytes(canonical(bad))
        with self.assertRaises(releases.UpdateBlocked):releases.policy_file(path,hashlib.sha256(path.read_bytes()).hexdigest())

    def test_http_error_filters_raw_debug_headers_and_secret_text(self):
        client=self.paired();token=self.backend.record['credential']
        class Fail:
            def open(s,request,**kwargs):raise HTTPError(request.full_url,401,'ignored',{},io.BytesIO(json.dumps({'request_id':None,'error':{'code':'device_authentication_failed','retryable':False,'debug':token},'raw':token}).encode()))
        client.transport.opener=Fail();result=client.request({'request_id':'synthetic_request_123456','method':'content_catalog','params':{'kinds':['memory']}})
        self.assertEqual(result['error'],{'code':'device_authentication_failed','retryable':False});self.assertNotIn(token,json.dumps(result))

    def test_https_pinned_release_health_and_helper_source_change_retains_active(self):
        import shutil
        import test_client_lifecycle as fixtures
        import client_release as releases
        import client_startup
        from build_client import release
        from types import SimpleNamespace
        f=fixtures.Releases('test_orphan_allowlist_discovery_install_upgrade_and_current');f.setUp();self.addCleanup(f.doCleanups)
        content=self.base/'content.json';content.write_bytes(canonical({'schema':1,'adapter':'codex','kinds':GRANT['artifact_types'],'ids':GRANT['ids'],'dependencies':{'commands':[],'python_modules':[]}}));content.chmod(0o600)
        runtime={'https_profile':str(self.profile_path),'https_profile_sha256':self.digest,'credential_source_sha256':self.profile['credential_source_sha256'],'content_policy':str(content),'content_policy_sha256':hashlib.sha256(content.read_bytes()).hexdigest(),'device_home':str(self.home),'codex_home':str(self.home/'codex'),'content_cache':str(self.base/'content-cache')}
        policy=copy.deepcopy(f.policy);policy.update(capabilities=HTTPS_CONTENT_CAPABILITIES,file_prefixes=list(HTTPS_CONTENT_CLIENT_FILES),runtime=runtime)
        path=self.base/'https-release-policy.json';path.write_bytes(canonical(policy));path.chmod(0o600);digest=hashlib.sha256(path.read_bytes()).hexdigest();channel=releases.ClientReleases(path,digest)
        def publish(version,mutate=False):
            out=self.base/('https-release-'+version);manifest=release(ROOT,out,version,'personal-content-https')
            if mutate:
                source=out/'native/CredentialStore.swift';source.write_bytes(source.read_bytes()+b'\n// SYNTHETIC_UNAPPROVED_HELPER_CHANGE\n');manifest['files']['native/CredentialStore.swift']=hashlib.sha256(source.read_bytes()).hexdigest();(out/'releases/manifest.json').write_text(json.dumps(manifest))
            for name in [*HTTPS_CONTENT_CLIENT_FILES,'releases/manifest.json']:
                destination=f.publisher/name;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(out/name,destination)
            f.git(f.publisher,'add','--',*HTTPS_CONTENT_CLIENT_FILES,'releases/manifest.json');f.git(f.publisher,'commit','-m','synthetic HTTPS '+version);f.git(f.publisher,'push','origin','releases/stable')
        with patch.object(NativeKeychain,'call',side_effect=AssertionError('Package health cannot query credentials')):
            publish('0.2.2');result=channel.update();self.assertEqual(result['status'],'updated',result);active=channel.verified_active()
            publish('0.2.3',True);self.assertEqual(channel.update()['status'],'release-review-required');self.assertEqual(channel.verified_active(),active)
        fixed=SimpleNamespace(policy=policy,verified_active=lambda:active,update=lambda:{'status':'offline','active':active});commands=[]
        with patch.object(client_startup,'ClientReleases',return_value=fixed),patch.object(client_startup.subprocess,'run',side_effect=lambda argv,**kw:commands.append(argv) or SimpleNamespace(returncode=0)):
            self.assertTrue(client_startup.startup(path,digest,installation_check=True)['served'])
        self.assertIn('--https-profile-sha256',commands[0]);self.assertIn('--credential-source-sha256',commands[0]);self.assertNotIn('--token-file',commands[0])

    def test_pairing_transport_terminal_redacts_error_and_does_not_follow_redirect(self):
        transport=PairingHTTPS('https://synthetic.invalid');token='SYNTHETIC_DEVICE_TOKEN_1234567890123456'
        class Fail:
            def open(s,request,**kwargs):raise HTTPError(request.full_url,403,'ignored',{},io.BytesIO(json.dumps({'error':{'code':'device_revoked','debug':token}}).encode()))
        transport.opener=Fail()
        from client_pairing import EnrollmentTerminal
        with self.assertRaises(EnrollmentTerminal) as caught:transport.confirm(token,'SYNTHETIC_NONCE_1234567890123456789012')
        self.assertNotIn(token,str(caught.exception));self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://elsewhere.invalid'))
        with self.assertRaises(ValueError):RemoteClient('http://127.0.0.1',credential_provider=lambda:token)

    def test_native_bridge_secrets_only_in_private_pipe_and_no_ambient_injection(self):
        helper=self.home/'helper';helper.write_bytes(b'SYNTHETIC_UNEXECUTED_NATIVE_HELPER');helper.chmod(0o700);seen=[]
        def run(argv,**kwargs):
            seen.append((argv,kwargs));return type('Reply',(),{'returncode':0,'stdout':b'{"saved":true}'})()
        backend=NativeKeychain(helper,hashlib.sha256(helper.read_bytes()).hexdigest(),'agent-brain.https.'+self.digest,runner=run)
        with patch('sys.platform','darwin'),patch.dict(os.environ,DYLD_INSERT_LIBRARIES='SYNTHETIC_INJECTION',HTTPS_PROXY='https://synthetic.invalid'):
            # /private/tmp is world-writable, so this fixture skips only ancestor
            # ownership checks; production validates every actual parent.
            original=Path.stat
            def stat(path,*args,**kwargs):
                result=original(path,*args,**kwargs)
                if path in helper.parents:
                    values=list(result);values[0]&=~0o022;return os.stat_result(values)
                return result
            with patch.object(Path,'stat',stat):backend.save({'credential':self.code},None)
        self.assertEqual(seen[0][0],[str(helper)]);self.assertIn(self.code.encode(),seen[0][1]['input']);self.assertNotIn(self.code,json.dumps(seen[0][1]['env']));self.assertNotIn('DYLD_INSERT_LIBRARIES',seen[0][1]['env']);self.assertNotIn('HTTPS_PROXY',seen[0][1]['env'])

    def test_https_export_separate_allowlist_no_server_signed_component_or_stable_manifest(self):
        out=self.base/'export';manifest=export(ROOT,out,https_content_candidate=True)
        self.assertEqual(set(manifest['files']),set(HTTPS_CONTENT_CLIENT_FILES));self.assertEqual(len(manifest['files']),21);self.assertFalse(manifest['keychain_accessed']);self.assertFalse((out/'releases').exists());self.assertFalse((out/'native/DeviceComponent.swift').exists());self.assertFalse(any('brain_server' in path for path in manifest['files']))
        with self.assertRaises(ValueError):export(ROOT,self.base/'mixed',ssh_content_candidate=True,https_content_candidate=True)

    def install_fixture(self):
        import content_install as install
        native=self.home/'codex';native.mkdir(mode=0o700);config=native/'config.toml';config.write_text('model="synthetic"\n');config.chmod(0o600)
        package=self.base/'install-package';export(ROOT,package,https_content_candidate=True)
        content=self.base/'install-content.json';content.write_bytes(canonical({'schema':1,'adapter':'codex','kinds':GRANT['artifact_types'],'ids':GRANT['ids'],'dependencies':{'commands':[],'python_modules':[]}}));content.chmod(0o600)
        runtime={'https_profile':str(self.profile_path),'https_profile_sha256':self.digest,'credential_source_sha256':self.profile['credential_source_sha256'],'content_policy':str(content),'content_policy_sha256':hashlib.sha256(content.read_bytes()).hexdigest(),'device_home':str(self.home),'codex_home':str(native),'content_cache':str(self.base/'install-cache')}
        policy={'schema':1,'adapter':'codex','platform':GRANT['platform'],'origin':'https://synthetic.invalid/client.git','ref':'refs/heads/releases/stable','anchor_commit':'a'*40,'channel':'stable','core_api':1,'state_schema':1,'capabilities':HTTPS_CONTENT_CAPABILITIES,'file_prefixes':list(HTTPS_CONTENT_CLIENT_FILES),'repository':str(self.base/'install-repository'),'cache':str(self.base/'install-release-cache'),'runtime':runtime}
        path=self.base/'install-release-policy.json';path.write_bytes(canonical(policy));path.chmod(0o600)
        plan=install.prepare(self.home,package,path,hashlib.sha256(path.read_bytes()).hexdigest());return install,plan,install.sha(install.contract.encode(plan)),config

    def test_install_once_digest_and_preimage_rejected_before_any_credential_input(self):
        install,plan,digest,config=self.install_fixture();before=config.read_bytes()
        def blocked():raise AssertionError('No secure input before native plan checks')
        result=install_once(plan,'0'*64,store=object(),secure_input=blocked);self.assertFalse(result['installed']);self.assertEqual(config.read_bytes(),before)
        config.write_text('independent-model="synthetic"\n');result=install_once(plan,digest,store=object(),secure_input=blocked);self.assertFalse(result['installed']);self.assertEqual(config.read_text(),'independent-model="synthetic"\n');self.assertEqual(self.prompts,0)

    def test_install_once_lost_reply_native_rollback_resume_and_complete_no_reprompt(self):
        install,plan,digest,config=self.install_fixture();before=config.read_bytes();checks=[]
        def check(value):
            checks.append(value)
            if len(checks)==2:raise ValueError('Synthetic failed native postcheck')
        installer=lambda p,d:install.apply(p,d,check)
        kwargs={'store':self.store,'transport':self.transport,'secure_input':self.input,'installer':installer}
        self.transport.lost_prepare=True;result=install_once(plan,digest,**kwargs);self.assertEqual(result['status'],'pairing-resume-required');self.assertEqual(config.read_bytes(),before)
        result=install_once(plan,digest,**kwargs);self.assertEqual(result['status'],'content-install-rolled-back');self.assertEqual(self.backend.record['phase'],'active');self.assertEqual(config.read_bytes(),before)
        result=install_once(plan,digest,**kwargs);self.assertTrue(result['installed']);self.assertEqual(self.prompts,1);self.assertNotIn(self.backend.record['credential'],json.dumps(result))
        with patch.object(install,'inspect',return_value={'status':'content-native-client-installed','installed':True,'native_changed':False}):
            result=install_once(plan,digest,**kwargs);self.assertTrue(result['installed']);self.assertFalse(result['native_changed']);self.assertEqual(self.prompts,1)

    def test_install_once_interrupted_native_transaction_recovers_without_reprompt(self):
        install,plan,digest,config=self.install_fixture()
        kwargs={'store':self.store,'transport':self.transport,'secure_input':self.input,
                'installer':lambda p,d:install.apply(p,d,lambda value:None)}
        self.assertTrue(install_once(plan,digest,**kwargs)['installed'])
        after=config.read_bytes();credential=copy.deepcopy(self.backend.record)
        receipt=self.home/'.config/agent-brain-client/content-install-receipt.json'
        journal=install.private_json(receipt);journal['phase']='applying';install.save(receipt,journal)
        result=install_once(plan,digest,**kwargs)
        self.assertTrue(result['installed'],result);self.assertEqual(config.read_bytes(),after)
        self.assertEqual(install.private_json(receipt)['phase'],'complete')
        self.assertEqual(self.backend.record,credential);self.assertEqual(self.prompts,1)

    def test_install_once_cancel_leaves_native_config_credential_and_network_untouched(self):
        install,plan,digest,config=self.install_fixture();before=config.read_bytes();result=install_once(plan,digest,store=self.store,transport=self.transport,secure_input=lambda:None)
        self.assertEqual(result['status'],'pairing-cancelled');self.assertEqual(config.read_bytes(),before);self.assertIsNone(self.backend.record);self.assertFalse(self.transport.prepares)

if __name__=='__main__':unittest.main()
