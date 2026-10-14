"""Synthetic native ownership, serialized plans and fixed-bootstrap regressions."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_content_install as fixtures
import client_startup
import content_install as install

class InstallBoundaries(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ContentInstall('test_prepare_read_only_then_apply_inspect_reuse_and_single_link_state')
        self.f.setUp();self.addCleanup(self.f.doCleanups)

    def apply(self):
        plan=json.loads(install.contract.encode(self.f.plan()))
        self.assertTrue(install.apply(plan,self.f.digest(plan),lambda p:None)['installed'])
        return plan

    def startup(self):
        fixed=SimpleNamespace(policy=self.f.policy,verified_active=lambda:self.f.active,update=lambda:{'status':'current','active':self.f.active})
        with patch.object(client_startup,'ClientReleases',return_value=fixed),patch.object(client_startup.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as launch:
            result=client_startup.startup(self.f.policy_path,self.f.policy_sha)
        return result,launch.call_count

    def test_json_roundtrip_has_identical_rendered_postimage_and_receipt(self):
        original=self.f.plan();reloaded=json.loads(install.contract.encode(original))
        before=self.f.config.read_bytes()
        self.assertEqual(install.render(before,original['mcp'],False),install.render(before,reloaded['mcp'],False))
        self.assertTrue(install.apply(reloaded,self.f.digest(reloaded),lambda p:None)['installed'])
        self.assertEqual(install.sha(self.f.config.read_bytes()),original['config_after_sha256'])
        receipt=json.loads((self.f.home/'.config/agent-brain-client/content-install-receipt.json').read_text())
        self.assertEqual(receipt['after'][str(self.f.config)],original['config_after_sha256'])

    def test_unrelated_model_and_other_server_changes_allow_startup_and_reentry_without_writes(self):
        plan=self.apply();receipt=self.f.home/'.config/agent-brain-client/content-install-receipt.json';old_receipt=receipt.read_bytes()
        text=self.f.config.read_text().replace('synthetic-fixture','synthetic-model-changed').replace('preserve-other','synthetic-other-updated')
        self.f.config.write_text(text);before=self.f.native_files()
        self.assertTrue(install.inspect(self.f.home)['installed'])
        result,calls=self.startup();self.assertTrue(result['served']);self.assertEqual(calls,1)
        self.assertTrue(install.apply(plan,self.f.digest(plan),lambda p:self.fail('Completed reentry must not rerun protocol/mutate'))['installed'])
        self.assertEqual(self.f.native_files(),before);self.assertEqual(receipt.read_bytes(),old_receipt)

    def test_every_owned_mcp_command_argument_and_permission_drift_blocks_startup(self):
        plan=self.apply();original=self.f.config.read_bytes()
        mutations={'command':'synthetic-unapproved','args':['synthetic-unapproved'],'enabled':False,'enabled_tools':['content_catalog','content_remove'],'default_tools_approval_mode':'auto','tool_timeout_sec':999,'env':{'SYNTHETIC':'unapproved'}}
        for key,value in mutations.items():
            with self.subTest(field=key):
                changed=copy.deepcopy(plan['mcp']);changed[key]=value
                if key=='env':
                    env_table='[mcp_servers.'+install.NAME+'.env]'
                    self.f.config.write_bytes(original+bytes([10])+env_table.encode()+bytes([10])+b'SYNTHETIC = "unapproved"'+bytes([10]))
                else:
                    self.f.config.write_bytes(install.render(original,changed,True))
                self.assertFalse(install.inspect(self.f.home)['installed'])
                result,calls=self.startup();self.assertEqual(result['status'],'content-native-install-pending');self.assertEqual(calls,0)
        self.f.config.write_bytes(original)

    def test_registration_and_policy_bindings_remain_strict(self):
        self.apply();reg=self.f.home/'.config/agent-brain-client/codex-mcp.json';original=reg.read_bytes()
        for key,value in {'mcp_sha256':'a'*64,'package_sha256':'b'*64,'release_policy_sha256':'c'*64,'plan_sha256':'d'*64}.items():
            with self.subTest(field=key):
                meta=json.loads(original);meta[key]=value;reg.write_bytes(install.contract.encode(meta))
                self.assertFalse(install.inspect(self.f.home)['installed'])
        reg.write_bytes(original)
        self.f.policy_path.write_bytes(self.f.policy_path.read_bytes()+b' ')
        self.assertFalse(install.inspect(self.f.home)['installed'])

    def test_interrupted_serialized_install_recovers_and_postcheck_preserves_outside_model_edit(self):
        plan=json.loads(install.contract.encode(self.f.plan()));real=install.atomic
        def crash(path,*args):
            real(path,*args)
            if path==self.f.config:raise SystemExit('synthetic interruption')
        with patch.object(install,'atomic',side_effect=crash):
            with self.assertRaises(SystemExit):install.apply(plan,self.f.digest(plan),lambda p:None)
        calls=[]
        def outside_model_edit(p):
            calls.append(True)
            if len(calls)==2:
                self.f.config.write_text(self.f.config.read_text().replace('synthetic-fixture','synthetic-user-model'))
                raise ValueError('synthetic failed postcheck')
        result=install.apply(plan,self.f.digest(plan),outside_model_edit)
        self.assertEqual(result['status'],'content-install-recovery-review-required')
        current=self.f.config.read_bytes();self.assertIn(b'synthetic-user-model',current)
        self.assertEqual(install.apply(plan,self.f.digest(plan),lambda p:None)['status'],'content-install-recovery-review-required')
        self.assertEqual(self.f.config.read_bytes(),current)

    def test_config_edit_between_render_and_journal_preimage_is_rejected(self):
        plan=self.f.plan();real=install.raw;calls=[]
        def race(path, *args, **kwargs):
            if Path(path)==self.f.config:
                calls.append(True)
                # prepare() and rendering have already observed the approved image.
                if len(calls)==3:
                    self.f.config.write_text(self.f.config.read_text().replace(
                        'synthetic-fixture','synthetic-raced-model'))
            return real(path, *args, **kwargs)
        with patch.object(install,'raw',side_effect=race):
            with self.assertRaisesRegex(install.InstallBlocked,'Preimage race'):
                install.apply(plan,self.f.digest(plan),lambda p:None)
        self.assertIn('synthetic-raced-model',self.f.config.read_text())
        self.assertFalse((self.f.home/'.config/agent-brain-client/codex-mcp.json').exists())
        self.assertFalse((self.f.home/'.config/agent-brain-client/content-install-receipt.json').exists())

    def test_fixed_bootstrap_process_imports_its_reviewed_dependency_after_model_change(self):
        self.apply();self.f.config.write_text(self.f.config.read_text().replace('synthetic-fixture','synthetic-model-changed'))
        fixed=self.f.base/'synthetic-fixed-bootstrap';shutil.copytree(self.f.package,fixed)
        payload={'fixed':str(fixed),'home':str(self.f.home),'active':self.f.active,'policy':self.f.policy,'policy_file':str(self.f.policy_path),'policy_sha':self.f.policy_sha}
        code="""import json,platform,sys\nfrom pathlib import Path\nfrom types import SimpleNamespace\np=json.loads(sys.stdin.read());sys.path.insert(0,str(Path(p['fixed'])/'adapters/codex'));sys.path.insert(0,str(Path(p['fixed'])/'scripts'));sys.platform='darwin';platform.machine=lambda:'arm64'\nimport client_startup,content_install\ncommands=[]\nclient_startup.ClientReleases=lambda *a:SimpleNamespace(policy=p['policy'],verified_active=lambda:p['active'],update=lambda:{'status':'current','active':p['active']})\nclient_startup.subprocess.run=lambda command,**kw:commands.append(command) or SimpleNamespace(returncode=0)\nresult=client_startup.startup(p['policy_file'],p['policy_sha'])\nprint(json.dumps({'served':result.get('served',False),'dependency_is_fixed_package':Path(content_install.__file__).parent==Path(p['fixed'])/'adapters/codex','launch_count':len(commands),'model_preserved':'synthetic-model-changed' in (Path(p['home'])/'.codex/config.toml').read_text()}))\n"""
        run=subprocess.run([sys.executable,'-I','-B','-c',code],input=json.dumps(payload),capture_output=True,text=True,timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(json.loads(run.stdout),{'served':True,'dependency_is_fixed_package':True,'launch_count':1,'model_preserved':True})

if __name__=='__main__':unittest.main()
