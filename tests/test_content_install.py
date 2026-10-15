"""Candidate install/update against fake homes, synthetic Git and fake protocol checks."""
import copy
import io
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_client_lifecycle as fixtures
ROOT=fixtures.ROOT
from build_client import release as build
import client_release as releases
import client_startup
import content_install as install
from content_mcp import installation_check
import connect as remote_connect

class ContentInstall(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.Releases('test_orphan_allowlist_discovery_install_upgrade_and_current');self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture;self.base=f.base.resolve();self.home=self.base/'fake-home';self.home.mkdir()
        self.native=self.home/'.codex';self.native.mkdir();self.config=self.native/'config.toml';self.config.write_text('model="synthetic-fixture"\n[mcp_servers.other]\ncommand="preserve-other"\n')
        self.profile=self.base/'profile.json';self.component=self.base/'synthetic-component';self.component.write_bytes(b'UNEXECUTED_SYNTHETIC_COMPONENT');self.component.chmod(0o600)
        self.grant={'actor':'synthetic-owner','scopes':['read','write'],'artifact_types':['memory','rule','skill'],'ids':{'memory':'*','rule':['synthetic-rule'],'skill':['synthetic-skill']},'adapter':'codex','platform':'darwin-arm64'}
        self.profile.write_text(json.dumps({'schema':1,'transport':'https','endpoint':'https://synthetic.invalid','adapter':'codex','platform':'darwin-arm64','data_mode':'personal','access_group':'TESTTEAM12.com.example.agentbrain','grant':self.grant}));self.profile.chmod(0o600)
        self.device_plan=self.base/'device-plan.json';self.device_plan.write_text(json.dumps({'schema':1,'adapter':'codex','platform':'darwin-arm64','profile':str(self.profile),'profile_sha256':install.sha(self.profile.read_bytes()),'component':str(self.component),'component_sha256':install.sha(self.component.read_bytes())}));self.device_plan.chmod(0o600)
        self.signing=self.base/'signing.json';self.signing.write_text(json.dumps({'identifier':'com.example.agentbrain','team_id':'TESTTEAM12'}));self.signing.chmod(0o600)
        self.content=self.base/'content.json';self.content_value={'schema':1,'adapter':'codex','kinds':['memory','rule','skill'],'ids':self.grant['ids'],'dependencies':{'commands':[],'python_modules':[]}};self.content.write_text(json.dumps(self.content_value));self.content.chmod(0o600)
        self.policy=copy.deepcopy(f.policy);self.policy.update(capabilities=releases.CONTENT_CAPABILITIES,file_prefixes=list(releases.CONTENT_CLIENT_FILES),repository=str(f.repository.resolve()),cache=str(self.base/'runtime'),runtime={'device_plan':str(self.device_plan),'device_plan_sha256':install.sha(self.device_plan.read_bytes()),'signing_policy':str(self.signing),'signing_policy_sha256':install.sha(self.signing.read_bytes()),'content_policy':str(self.content),'content_policy_sha256':install.sha(self.content.read_bytes()),'device_home':str(self.home),'codex_home':str(self.native),'content_cache':str(self.base/'content-cache')})
        self.policy['runtime']['component_source_sha256']=install.sha((ROOT/'native/DeviceComponent.swift').read_bytes())
        self.policy_path=self.base/'approved-policy.json';self.policy_path.write_text(json.dumps(self.policy));self.policy_path.chmod(0o600);self.policy_sha=install.sha(self.policy_path.read_bytes())
        self.channel=releases.ClientReleases(self.policy_path,self.policy_sha);self.publish();self.active=self.channel.update()['active'];self.package=Path(self.active['package'])
        self.platform=patch('sys.platform','darwin');self.platform.start();self.addCleanup(self.platform.stop)
        self.machine=patch('platform.machine',return_value='arm64');self.machine.start();self.addCleanup(self.machine.stop)

    def publish(self,version='0.2.0',mutate=None):
        f=self.fixture;out=self.base/('content-release-'+version);manifest=build(ROOT,out,version,'personal-content')
        if mutate:mutate(manifest,out)
        (out/'releases/manifest.json').write_text(json.dumps(manifest))
        for name in [*releases.CONTENT_CLIENT_FILES,'releases/manifest.json']:
            destination=f.publisher/name;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(out/name,destination)
        f.git(f.publisher,'add','--',*releases.CONTENT_CLIENT_FILES,'releases/manifest.json');f.git(f.publisher,'commit','-m','synthetic content '+version);f.git(f.publisher,'push','origin','releases/stable')

    def plan(self,replace=False):return install.prepare(self.home,self.package,self.policy_path,self.policy_sha,replace)
    def digest(self,plan):return install.sha(install.contract.encode(plan))
    def native_files(self):return {p.relative_to(self.home).as_posix():p.read_bytes() for p in self.home.rglob('*') if p.is_file() and p.suffix!='.lock'}

    def test_prepare_read_only_then_apply_inspect_reuse_and_single_link_state(self):
        before=self.native_files();plan=self.plan();self.assertEqual(self.native_files(),before);checks=[]
        report=install.apply(plan,self.digest(plan),lambda p:checks.append(p));self.assertTrue(report['installed']);self.assertTrue(report['protocol_checked']);self.assertEqual(report['model_loaded'],'unknown');self.assertEqual(len(checks),2)
        cfg=install.tomllib.loads(self.config.read_text());self.assertEqual(cfg['model'],'synthetic-fixture');self.assertEqual(cfg['mcp_servers']['other'],{'command':'preserve-other'});self.assertEqual(cfg['mcp_servers'][install.NAME]['default_tools_approval_mode'],'writes')
        self.assertEqual(install.inspect(self.home)['installed_version'],'0.2.0');self.assertTrue(install.apply(plan,self.digest(plan),lambda p:checks.append(p))['installed']);self.assertEqual(len(checks),2)
        self.assertEqual(remote_connect.connect(self.home)['status'],'content-native-client-installed');self.assertEqual(remote_connect.connect(self.home)['installed_version'],'0.2.0');self.assertFalse((self.home/'.agents').exists())

    def test_private_plan_single_link_inspection_and_pending_startup_never_maps(self):
        plan=self.plan();path=self.base/'native-plan.json';install.save(path,plan);before=self.native_files()
        self.assertEqual(install.inspect_plan(path,self.digest(plan))['status'],'content-native-install-ready');self.assertEqual(self.native_files(),before)
        output=io.StringIO()
        with patch.object(sys,'argv',['connect.py','--agent','codex','--content-install-plan',str(path),'--plan-sha256',self.digest(plan)]),patch.object(sys,'stdout',output):
            self.assertEqual(remote_connect.main(),0)
        self.assertEqual(json.loads(output.getvalue())['status'],'content-native-install-ready')
        fixed=SimpleNamespace(policy=self.policy,verified_active=lambda:self.active,update=lambda:{'status':'current','active':self.active})
        with patch.object(client_startup,'ClientReleases',return_value=fixed),patch.object(client_startup.subprocess,'run') as launch:
            self.assertEqual(client_startup.startup(self.policy_path,self.policy_sha)['status'],'content-native-install-pending');launch.assert_not_called()

    def test_failed_postcheck_restores_exact_native_config_and_no_registration(self):
        before=self.config.read_bytes();mode=self.config.stat().st_mode&0o777;calls=[]
        def fail_after(p):
            calls.append(p)
            if len(calls)==2:raise RuntimeError('Synthetic protocol failure')
        result=install.apply(self.plan(),self.digest(self.plan()),fail_after)
        self.assertEqual(result['status'],'content-install-rolled-back');self.assertEqual(self.config.read_bytes(),before);self.assertEqual(self.config.stat().st_mode&0o777,mode);self.assertFalse((self.home/'.config/agent-brain-client/codex-mcp.json').exists())

    def test_final_completion_receipt_save_failure_rolls_back_native_files(self):
        plan=self.plan();before=self.native_files();real_save=install.save
        def fail_complete(path,value):
            if value.get('phase')=='complete':raise OSError('Synthetic completion receipt failure')
            return real_save(path,value)
        with patch.object(install,'save',side_effect=fail_complete):
            result=install.apply(plan,self.digest(plan),lambda p:None)
        self.assertEqual(result['status'],'content-install-rolled-back')
        after=self.native_files();after.pop('.config/agent-brain-client/content-install-receipt.json')
        self.assertEqual(after,before)
        receipt=self.home/'.config/agent-brain-client/content-install-receipt.json'
        self.assertEqual(install.private_json(receipt)['phase'],'rolled-back')

    def test_postcheck_external_edit_blocks_recovery_and_preserves_both(self):
        plan=self.plan();calls=[]
        def drift(p):
            calls.append(p)
            if len(calls)==2:self.config.write_text(self.config.read_text()+'\nexternal="preserve"\n');raise RuntimeError('Synthetic outside edit')
        result=install.apply(plan,self.digest(plan),drift);self.assertEqual(result['status'],'content-install-recovery-review-required');self.assertIsNone(result['native_changed'])
        before=self.native_files();self.assertEqual(install.apply(plan,self.digest(plan),lambda p:None)['status'],'content-install-recovery-review-required');self.assertEqual(self.native_files(),before)

    def test_interrupted_apply_recovers_before_retry(self):
        plan=self.plan();real=install.atomic;calls=[]
        def crash(path,*args):
            result=real(path,*args)
            if path==self.config:
                calls.append(path)
                if len(calls)==1:raise SystemExit('Synthetic interruption')
            return result
        with patch.object(install,'atomic',side_effect=crash):
            with self.assertRaises(SystemExit):install.apply(plan,self.digest(plan),lambda p:None)
        result=install.apply(plan,self.digest(plan),lambda p:None);self.assertTrue(result['installed']);self.assertTrue(install.inspect(self.home)['installed'])

    def test_input_drift_wrong_digest_and_unknown_existing_binding_preserved(self):
        plan=self.plan();before=self.native_files()
        self.assertEqual(install.apply(plan,'0'*64,lambda p:None)['status'],'content-install-plan-review-required');self.assertEqual(self.native_files(),before)
        self.content.write_text(self.content.read_text()+' ')
        with self.assertRaises(ValueError):install.apply(plan,self.digest(plan),lambda p:None)
        self.assertEqual(self.native_files(),before)

    def test_owned_existing_mcp_requires_explicit_replacement_and_rolls_back(self):
        old={'command':'synthetic-old-command','args':['fixed-only'],'enabled':True}
        self.config.write_text(self.config.read_text()+'\n[mcp_servers.'+install.NAME+']\n'+''.join(k+' = '+json.dumps(v)+'\n' for k,v in old.items()))
        with self.assertRaises(ValueError):self.plan()
        with self.assertRaises(ValueError):self.plan(True)
        registry=self.home/'.config/agent-brain-client/codex-mcp.json';registry.parent.mkdir(parents=True,mode=0o700);registry.write_text(json.dumps({'schema':1,'native_mcp_name':install.NAME,'mcp_sha256':install.sha(json.dumps(old,sort_keys=True).encode())}));registry.chmod(0o600)
        before=self.native_files();plan=self.plan(True);calls=[]
        def fail(p):
            calls.append(p)
            if len(calls)==2:raise RuntimeError('Synthetic')
        self.assertEqual(install.apply(plan,self.digest(plan),fail)['status'],'content-install-rolled-back')
        for name,raw in before.items():self.assertEqual((self.home/name).read_bytes(),raw)

    def test_installation_protocol_check_only_reads_catalog_and_never_maps(self):
        outer=self
        class Client:
            grant=outer.grant
            def request(c,request):
                outer.assertEqual(request['method'],'content_catalog');return {'request_id':request['request_id'],'result':{'content_api_version':1,'commit':'a'*40,'artifacts':[]},'persistence':{'state':'read-only'}}
        before=self.native_files();result=installation_check(Client(),self.content_value);self.assertTrue(result['authentication_checked']);self.assertEqual(set(result['tools']),{'content_catalog','content_read','content_put','content_sources_inventory'});self.assertFalse(result['native_changed']);self.assertEqual(self.native_files(),before)

    def test_content_release_discovery_update_failure_offline_and_startup_wiring(self):
        report=self.channel.discover('codex','darwin-arm64','0.1.0');self.assertEqual(report['status'],'compatible-update');self.assertEqual(len(report['releases'][0]['manifest']['files']),19)
        old=self.channel.engine.active();self.publish('0.2.1');self.assertEqual(self.channel.update()['status'],'updated');self.assertEqual(install.apply(self.plan(),self.digest(self.plan()),lambda p:None)['installed'],True);self.assertEqual(install.inspect(self.home)['installed_version'],'0.2.1')
        def bad(manifest,out):
            p=out/'adapters/codex/content_mcp.py';p.write_text('BAD PYTHON (');manifest['files']['adapters/codex/content_mcp.py']=install.sha(p.read_bytes())
        previous=self.channel.engine.active();self.publish('0.2.2',bad);self.assertEqual(self.channel.update()['status'],'release-review-required');self.assertEqual(self.channel.engine.active(),previous)
        with patch.object(self.channel.engine.source,'latest',side_effect=OSError('Synthetic offline')):
            self.assertEqual(self.channel.update()['status'],'offline');self.assertEqual(self.channel.engine.active(),previous)
        commands=[];fixed=SimpleNamespace(policy=self.policy,verified_active=lambda:previous,update=lambda:{'status':'offline','active':previous})
        with patch.object(client_startup,'ClientReleases',return_value=fixed),patch.object(client_startup.subprocess,'run',side_effect=lambda argv,**kw:commands.append(argv) or SimpleNamespace(returncode=0)):
            result=client_startup.startup(self.policy_path,self.policy_sha,installation_check=True)
        self.assertTrue(result['served']);self.assertIn('--installation-check',commands[-1]);self.assertIn('--device-plan-sha256',commands[-1]);self.assertNotIn('--token-file',commands[-1]);self.assertEqual(commands[-1][commands[-1].index('--client-version')+1],previous['version'])

    def test_old_policy_does_not_accept_expanded_scope(self):
        old=copy.deepcopy(self.policy);old['capabilities']=releases.PERSONAL_CAPABILITIES;old['runtime']={'endpoint':'http://127.0.0.1:8765','token_file':str(self.base/'unread.token')}
        self.policy_path.write_text(json.dumps(old))
        with self.assertRaises(releases.UpdateBlocked):releases.policy_file(self.policy_path,install.sha(self.policy_path.read_bytes()))

    def test_inspection_from_different_python_preserves_original_pinned_command(self):
        plan=self.plan();install.apply(plan,self.digest(plan),lambda p:None)
        before=self.native_files();original=plan['mcp']['command']
        other=self.base/'other-python';other.write_bytes(b'SYNTHETIC_UNUSED_INTERPRETER');other.chmod(0o700)
        with patch.object(sys,'executable',str(other)),patch.object(install.subprocess,'run',side_effect=AssertionError('Inspection executes nothing')):
            result=install.inspect(self.home)
        self.assertTrue(result['installed'],result);self.assertTrue(result['installation_present']);self.assertEqual(result['installation_state'],'verified')
        self.assertEqual(install.tomllib.loads(self.config.read_text())['mcp_servers'][install.NAME]['command'],original)
        self.assertEqual(self.native_files(),before)

    def test_real_mcp_command_drift_is_not_accepted_from_other_python(self):
        plan=self.plan();install.apply(plan,self.digest(plan),lambda p:None)
        self.config.write_text(self.config.read_text().replace(plan['mcp']['command'],'/synthetic/unapproved-python'))
        before=self.native_files()
        with patch.object(sys,'executable','/synthetic/unapproved-python'):result=install.inspect(self.home)
        self.assertFalse(result['installed']);self.assertTrue(result['installation_present']);self.assertEqual(result['reason'],'mcp-binding-changed')
        self.assertEqual(self.native_files(),before);self.assertNotIn('/synthetic/unapproved-python',json.dumps(result))

    def test_missing_original_interpreter_is_present_but_review_required(self):
        interpreter=self.base/'original-python';interpreter.write_bytes(b'SYNTHETIC_UNUSED_INTERPRETER');interpreter.chmod(0o700)
        with patch.object(sys,'executable',str(interpreter)):
            plan=self.plan();install.apply(plan,self.digest(plan),lambda p:None)
        interpreter.unlink()
        result=install.inspect(self.home)
        self.assertFalse(result['installed']);self.assertTrue(result['installation_present']);self.assertEqual(result['reason'],'interpreter-unavailable')
        self.assertIn('original',result['next_action']);self.assertTrue((self.home/'.config/agent-brain-client/codex-mcp.json').exists())

    def test_package_drift_reports_safe_checkpoint_without_repair(self):
        plan=self.plan();install.apply(plan,self.digest(plan),lambda p:None)
        target=self.package/'adapters/codex/content_cache.py';target.write_bytes(target.read_bytes()+b'\n# SYNTHETIC_UNAPPROVED_EDIT\n')
        result=install.inspect(self.home)
        self.assertFalse(result['installed']);self.assertTrue(result['installation_present']);self.assertEqual(result['reason'],'bootstrap-package-changed')
        self.assertNotIn('SYNTHETIC_UNAPPROVED_EDIT',json.dumps(result));self.assertTrue(target.read_bytes().endswith(b'UNAPPROVED_EDIT\n'))

    def test_absent_registration_is_distinct_from_preserved_recovery_materials(self):
        result=install.inspect(self.home)
        self.assertEqual(result['status'],'client-not-installed');self.assertFalse(result['installation_present'])
        plan=self.plan();install.apply(plan,self.digest(plan),lambda p:None)
        (self.home/'.config/agent-brain-client/codex-mcp.json').unlink()
        result=install.inspect(self.home)
        self.assertEqual(result['reason'],'registration-missing');self.assertFalse(result['installation_present'])
        self.assertEqual(result['installation_state'],'registration-missing-with-recovery-materials')

    def test_inspection_release_view_cannot_fetch_or_activate_updates(self):
        view=releases.ClientReleases(self.policy_path,self.policy_sha,inspection=True)
        before=self.native_files()
        with patch.object(view.engine,'check',side_effect=AssertionError('Read-only inspection cannot update')):
            self.assertEqual(view.update()['status'],'release-review-required')
        self.assertEqual(view.discover('codex','darwin-arm64','0.2.0')['status'],'release-review-required')
        self.assertEqual(self.native_files(),before)

    def test_valid_syntax_failed_package_health_restores_previous_active_version(self):
        old=self.channel.engine.active()
        def unhealthy(manifest,out):
            p=out/'adapters/codex/content_mcp.py';p.write_text('raise SystemExit(7)\n');manifest['files']['adapters/codex/content_mcp.py']=install.sha(p.read_bytes())
        self.publish('0.2.1',unhealthy);result=self.channel.update()
        self.assertEqual(result['status'],'release-review-required');self.assertEqual(self.channel.engine.active(),old);self.assertEqual(json.loads(self.channel.engine.journal.read_text())['phase'],'rolled-back')

    def test_native_component_source_change_requires_new_signed_plan_before_activation(self):
        old=self.channel.engine.active()
        def native_change(manifest,out):
            p=out/'native/DeviceComponent.swift';p.write_text(p.read_text()+'\n// synthetic changed native source\n');manifest['files']['native/DeviceComponent.swift']=install.sha(p.read_bytes())
        self.publish('0.2.1',native_change)
        self.assertEqual(self.channel.discover('codex','darwin-arm64','0.2.0')['status'],'release-review-required');self.assertEqual(self.channel.update()['status'],'release-review-required');self.assertEqual(self.channel.engine.active(),old)


    def test_failed_replacement_restores_completed_receipt_for_inspection(self):
        original_plan=self.plan()
        self.assertTrue(install.apply(original_plan,self.digest(original_plan),lambda p:None)['installed'])
        before=self.native_files()
        receipt_path=self.home/'.config/agent-brain-client/content-install-receipt.json'
        original_receipt=install.private_json(receipt_path)
        self.publish('0.2.1')
        update=self.channel.update();self.assertEqual(update['status'],'updated')
        self.package=Path(update['active']['package'])
        replacement=self.plan(replace=True);replacement_digest=self.digest(replacement)
        real_save=install.save
        def fail_replacement_completion(path,value):
            if value.get('phase')=='complete' and value.get('plan_sha256')==replacement_digest:
                raise OSError('Synthetic replacement completion receipt failure')
            return real_save(path,value)
        with patch.object(install,'save',side_effect=fail_replacement_completion):
            result=install.apply(replacement,replacement_digest,lambda p:None)
        self.assertEqual(result['status'],'content-install-rolled-back')
        self.assertEqual(self.native_files(),before)
        self.assertEqual(install.private_json(receipt_path),original_receipt)
        self.assertTrue(install.inspect(self.home)['installed'])

if __name__=='__main__':unittest.main()
