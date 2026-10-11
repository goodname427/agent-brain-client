"""SSH candidate reuses release/native recovery against a fake Mac home."""
import copy
import json
import shutil
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_content_install as fixtures
import client_release as releases
import content_install as install
import client_startup
from build_client import release as build
from ssh_content import PREFIX,SSHChannel

class SSHInstall(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ContentInstall('test_prepare_read_only_then_apply_inspect_reuse_and_single_link_state');self.f.setUp();self.addCleanup(self.f.doCleanups)
        f=self.f;self.base=f.base;self.patch=patch('ssh_content.platform.machine',return_value='arm64');self.patch.start();self.addCleanup(self.patch.stop)
        key=self.base/'unexecuted-synthetic-ssh-key';key.write_text('SYNTHETIC_NOT_A_KEY');key.chmod(0o600)
        known=self.base/'ssh-known';known.write_text('synthetic pinned host');known.chmod(0o600)
        value={'schema':1,'adapter':'codex','platform':'darwin-arm64','data_mode':'personal','transport':'ssh-fixed-proxy','grant':f.grant,'ssh':{'host':'synthetic.invalid','user':'ubuntu','port':22,'identity_file':str(key),'known_hosts_file':str(known),'known_hosts_sha256':install.sha(known.read_bytes()),'remote_command':PREFIX+'/opt/agent-brain-client/ssh-bootstrap/scripts/brain_ssh_proxy.py --policy-file /var/lib/agent-brain/auth/ssh-proxy.json --policy-sha256 '+'e'*64}}
        self.profile=self.base/'ssh-content-profile.json';self.profile.write_bytes(install.contract.encode(value));self.profile.chmod(0o600)
        self.policy=copy.deepcopy(f.policy);self.policy.update(capabilities=releases.SSH_CONTENT_CAPABILITIES,file_prefixes=list(releases.SSH_CONTENT_CLIENT_FILES),cache=str(self.base/'ssh-release-cache'),runtime={'ssh_profile':str(self.profile),'ssh_profile_sha256':install.sha(self.profile.read_bytes()),'content_policy':str(f.content),'content_policy_sha256':install.sha(f.content.read_bytes()),'device_home':str(f.home),'codex_home':str(f.native),'content_cache':str(self.base/'ssh-content-cache')})
        self.path=self.base/'ssh-release-policy.json';self.path.write_bytes(install.contract.encode(self.policy));self.path.chmod(0o600);self.digest=install.sha(self.path.read_bytes());self.publish('0.3.0')
        self.channel=releases.ClientReleases(self.path,self.digest);result=self.channel.update();self.assertEqual(result['status'],'updated',result);self.active=self.channel.verified_active();self.package=Path(self.active['package'])

    def publish(self,version,mutate=None):
        out=self.base/('ssh-content-'+version);manifest=build(fixtures.ROOT,out,version,'personal-content-ssh')
        if mutate:mutate(manifest,out)
        (out/'releases/manifest.json').write_text(json.dumps(manifest))
        for name in [*releases.SSH_CONTENT_CLIENT_FILES,'releases/manifest.json']:
            destination=self.f.fixture.publisher/name;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(out/name,destination)
        f=self.f.fixture;f.git(f.publisher,'add','--',*releases.SSH_CONTENT_CLIENT_FILES,'releases/manifest.json');f.git(f.publisher,'commit','-m','synthetic ssh content '+version);f.git(f.publisher,'push','origin','releases/stable')

    def plan(self):return install.prepare(self.f.home,self.package,self.path,self.digest)

    def test_install_protocol_gate_recovery_and_startup_without_signing_or_client_token(self):
        before=self.f.native_files();plan=self.plan();self.assertEqual(self.f.native_files(),before);calls=[]
        def fail(p):
            calls.append(p)
            if len(calls)==2:raise ValueError('synthetic postcheck')
        self.assertEqual(install.apply(plan,self.f.digest(plan),fail)['status'],'content-install-rolled-back');self.assertEqual(self.f.config.read_bytes(),before['.codex/config.toml'])
        self.assertTrue(install.apply(plan,self.f.digest(plan),lambda p:None)['installed']);self.assertEqual(install.inspect(self.f.home)['installed_version'],'0.3.0')
        commands=[];fixed=SimpleNamespace(policy=self.policy,update=lambda:{'status':'current','active':self.active},verified_active=lambda:self.active)
        with patch.object(client_startup,'ClientReleases',return_value=fixed),patch.object(client_startup.subprocess,'run',side_effect=lambda argv,**kw:commands.append(argv) or SimpleNamespace(returncode=0)):
            self.assertTrue(client_startup.startup(self.path,self.digest,installation_check=True)['served'])
        self.assertIn('--ssh-profile',commands[-1]);self.assertNotIn('--signing-policy',commands[-1]);self.assertNotIn('--token-file',commands[-1]);self.assertNotIn('native/DeviceComponent.swift',releases.SSH_CONTENT_CLIENT_FILES)

    def test_release_compatible_update_offline_health_rollback_and_capability_mixing(self):
        self.publish('0.3.1');self.assertEqual(self.channel.update()['status'],'updated');previous=self.channel.verified_active()
        def unhealthy(manifest,out):
            p=out/'adapters/codex/content_mcp.py';p.write_text('raise SystemExit(9)\n');manifest['files']['adapters/codex/content_mcp.py']=install.sha(p.read_bytes())
        self.publish('0.3.2',unhealthy);self.assertEqual(self.channel.update()['status'],'release-review-required');self.assertEqual(self.channel.verified_active(),previous)
        with patch.object(self.channel.engine.source,'latest',side_effect=OSError('synthetic offline')):self.assertEqual(self.channel.update()['status'],'offline')
        wrong=copy.deepcopy(self.policy);wrong['capabilities']=releases.CONTENT_CAPABILITIES;self.path.write_bytes(install.contract.encode(wrong))
        with self.assertRaises(releases.UpdateBlocked):releases.policy_file(self.path,install.sha(self.path.read_bytes()))

    def test_ssh_stdio_session_reuses_one_process_and_closes_without_background(self):
        import subprocess
        calls=[]
        def spawn(argv,**kw):
            calls.append(argv);return subprocess.Popen([sys.executable,'-I','-B','-u','-c','import sys\nfor line in sys.stdin.buffer:\n sys.stdout.buffer.write(line);sys.stdout.buffer.flush()'],**kw)
        channel=SSHChannel(spawn);self.addCleanup(channel.close)
        for raw in (b'{"synthetic":1}\n',b'{"synthetic":2}\n'):self.assertEqual(channel(['/usr/bin/ssh','fixed'],input=raw,capture_output=True,timeout=5).stdout,raw)
        self.assertEqual(len(calls),1);process=channel.process;channel.close();self.assertIsNotNone(process.poll());self.assertIsNone(channel.process)


    def test_owned_legacy_instruction_retirement_is_explicit_restorable_and_mapping_owned(self):
        from codex_connect import PERSONAL_MANAGED
        from content_cache import ContentAdapter
        agents=self.f.native/'AGENTS.md';agents.write_text('Keep unrelated user instructions\n'+PERSONAL_MANAGED)
        old={'command':'synthetic-old-fixed-ssh'};self.f.config.write_text(self.f.config.read_text()+'\n[mcp_servers.'+install.NAME+']\ncommand="synthetic-old-fixed-ssh"\n')
        registry=self.f.home/'.config/agent-brain-client/codex-mcp.json';registry.parent.mkdir(parents=True,exist_ok=True,mode=0o700);old_receipt=self.base/'old-migration.json'
        registry.write_bytes(install.contract.encode({'schema':1,'native_mcp_name':install.NAME,'migration_receipt':str(old_receipt)}));registry.chmod(0o600)
        old_receipt.write_bytes(install.contract.encode({'installation_complete':True,'native_registered':True,'new_registration_path':str(registry),'after':{str(p):install.sha(p.read_bytes()) for p in (self.f.config,registry,agents)}}));old_receipt.chmod(0o600)
        before=agents.read_bytes();plan=install.prepare(self.f.home,self.package,self.path,self.digest,True);self.assertEqual(len(plan['before']),3);self.assertIsNotNone(plan['legacy_agents']);calls=[]
        def fail(p):
            calls.append(p)
            if len(calls)==2:raise ValueError('synthetic failed check')
        self.assertEqual(install.apply(plan,self.f.digest(plan),fail)['status'],'content-install-rolled-back');self.assertEqual(agents.read_bytes(),before)
        self.assertTrue(install.apply(plan,self.f.digest(plan),lambda p:None)['installed']);self.assertEqual(agents.read_text(),'Keep unrelated user instructions\n')
        class Client:
            grant=self.f.grant
            def request(c,r):return {'request_id':r['request_id'],'result':{'content_api_version':1,'commit':'a'*40,'artifacts':[]}}
        adapter=ContentAdapter(self.f.home,self.policy['runtime']['content_cache'],self.f.content_value,Client(),codex_home=self.f.native)
        self.assertEqual(adapter.refresh()['status'],'content-refreshed');self.assertTrue(install.inspect(self.f.home)['installed']);agents.write_text(agents.read_text()+'External user edit\n');self.assertFalse(install.inspect(self.f.home)['installed'])

    def test_preserved_legacy_registration_and_paused_hooks_need_both_pinned_preimages(self):
        legacy=self.f.home/'.config/agent-brain/device.json';legacy.parent.mkdir(parents=True,exist_ok=True);legacy.write_text('{"root":"synthetic-other-binding"}')
        hooks=self.f.native/'hooks.json';hooks.write_text('{}\n');before=legacy.read_bytes()
        with self.assertRaises(ValueError):self.plan()
        self.policy['runtime'].update(legacy_binding_sha256=install.sha(before),legacy_hooks_sha256=install.sha(hooks.read_bytes()));self.path.write_bytes(install.contract.encode(self.policy));self.digest=install.sha(self.path.read_bytes());plan=self.plan()
        self.assertNotIn(str(legacy),plan['before']);self.assertNotIn(str(hooks),plan['before']);self.assertTrue(install.apply(plan,self.f.digest(plan),lambda p:None)['installed']);self.assertEqual(legacy.read_bytes(),before)
        commands=[];fixed=SimpleNamespace(policy=self.policy,update=lambda:{'status':'current','active':self.active},verified_active=lambda:self.active)
        with patch.object(client_startup,'ClientReleases',return_value=fixed),patch.object(client_startup.subprocess,'run',side_effect=lambda argv,**kw:commands.append(argv) or SimpleNamespace(returncode=0)):
            self.assertTrue(client_startup.startup(self.path,self.digest,installation_check=True)['served'])
        self.assertNotIn('--legacy-binding-sha256',commands[-1]);hooks.write_text('{"unknown":"preserve"}')
        self.assertFalse(install.inspect(self.f.home)['installed']);self.assertEqual(legacy.read_bytes(),before)

if __name__=='__main__':unittest.main()
