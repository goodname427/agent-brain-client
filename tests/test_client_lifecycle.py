"""One Mac checkpoint; disposable client-only Git/native state and fake auth."""
import copy
import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'));sys.path.insert(0,str(ROOT/'tools'));sys.path.insert(0,str(ROOT/'adapters/codex'))
import build_client as builder
import client_release as release
import client_startup
import codex_connect as native
import connect as remote_connect


class Releases(unittest.TestCase):
    def test_check_only_offline_requires_a_verified_active_package(self):
        args=['client_startup.py','--policy-file','unused-policy','--policy-sha256','a'*64,'--check-only']
        for active,expected in ((None,2),({'commit':'a'*40,'version':'0.1.1'},0)):
            result={'status':'offline','active':active}
            with patch.object(sys,'argv',args),patch.object(client_startup,'startup',return_value=result),patch.object(sys,'stdout',io.StringIO()):
                self.assertEqual(client_startup.main(),expected)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        self.remote=self.base/'client-code.git';self.publisher=self.base/'publisher';self.publisher.mkdir()
        self.git(self.base,'init','--bare',self.remote)
        self.git(self.publisher,'init','-b','releases/stable')
        self.git(self.publisher,'config','user.name','Synthetic release fixture');self.git(self.publisher,'config','user.email','fixture@example.invalid')
        (self.publisher/'README.md').write_text('Synthetic client code only\n');self.git(self.publisher,'add','README.md');self.git(self.publisher,'commit','-m','fresh orphan synthetic code anchor')
        anchor=self.git(self.publisher,'rev-parse','HEAD');self.git(self.publisher,'remote','add','origin',self.remote)
        self.git(self.publisher,'push','-u','origin','releases/stable')
        self.repository=self.base/'source';self.git(self.base,'clone','--branch','releases/stable',self.remote,self.repository)
        self.policy={'schema':1,'adapter':'codex','platform':'darwin-arm64','origin':str(self.remote),'ref':'refs/heads/releases/stable','anchor_commit':anchor,'channel':'stable','core_api':1,'state_schema':1,'capabilities':release.CAPABILITIES,'file_prefixes':list(release.CLIENT_FILES),'repository':str(self.repository),'cache':str(self.base/'runtime'),'runtime':{'endpoint':'http://127.0.0.1:8765','token_file':str(self.base/'never-read.token')}}
        self.policy_path=self.base/'approved-policy.json';self.policy_path.write_text(json.dumps(self.policy));self.policy_path.chmod(0o600)
        self.policy_sha=hashlib.sha256(self.policy_path.read_bytes()).hexdigest()
        self.channel=release.ClientReleases(self.policy_path,self.policy_sha)

    def git(self,cwd,*args):
        env=dict(os.environ,GIT_CONFIG_GLOBAL=os.devnull,GIT_CONFIG_NOSYSTEM='1',GIT_TERMINAL_PROMPT='0')
        return subprocess.check_output(['git','-c','core.hooksPath='+os.devnull,'-C',str(cwd),*map(str,args)],env=env,stderr=subprocess.DEVNULL,text=True).strip()

    def publish(self,version='0.1.1',mutate=None,code=None):
        output=self.base/('candidate-'+str(len(list(self.base.glob('candidate-*')))))
        manifest=builder.release(ROOT,output,version)
        if code:
            name,raw=code;(output/name).write_bytes(raw);manifest['files'][name]=hashlib.sha256(raw).hexdigest()
        if mutate:mutate(manifest)
        (output/'releases/manifest.json').write_text(json.dumps(manifest))
        for p in output.rglob('*'):
            if p.is_file() and p.name!='export-manifest.json':
                dst=self.publisher/p.relative_to(output);dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,dst)
        self.git(self.publisher,'add','--',*release.CLIENT_FILES,'releases/manifest.json');self.git(self.publisher,'commit','-m','synthetic release '+version);self.git(self.publisher,'push','origin','releases/stable')
        return manifest

    def test_orphan_allowlist_discovery_install_upgrade_and_current(self):
        manifest=self.publish();report=self.channel.discover('codex','darwin-arm64','0.1.0')
        self.assertEqual(report['status'],'compatible-update');self.assertEqual(report['releases'][0]['manifest'],manifest)
        self.assertIsNone(self.channel.engine.active(),'catalog never activates executable code')
        self.assertEqual(set(manifest['files']),set(release.CLIENT_FILES));self.assertFalse((self.publisher/'memory').exists())
        first=self.channel.update();self.assertEqual(first['status'],'updated');self.assertEqual(first['active']['version'],'0.1.1')
        self.assertEqual(self.channel.update()['status'],'current')
        self.publish('0.1.2');second=self.channel.update();self.assertEqual(second['status'],'updated');self.assertEqual(second['active']['version'],'0.1.2')
        self.assertNotEqual(first['active']['package'],second['active']['package']);self.assertTrue(Path(first['active']['package']).is_dir())

    def test_scope_protocol_dependency_and_migration_changes_require_review(self):
        self.publish();old=self.channel.update()['active']
        changes=[lambda m:m.update(core_api=2),lambda m:m.update(capabilities=['real-memory']),lambda m:m.update(python_min='99.0'),lambda m:m['files'].pop('README.md'),lambda m:m['files'].update({'memory/private.md':'0'*64}),lambda m:m.update(migrations=[{'id':'unreviewed','sha256':'0'*64}])]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                self.publish('0.1.2',change);self.assertEqual(self.channel.update()['status'],'release-review-required');self.assertEqual(self.channel.engine.active(),old)

    def test_hash_or_source_drift_preserves_active_package(self):
        self.publish();old=self.channel.update()['active']
        self.publish('0.1.2',lambda m:m['files'].update({'connect.py':'0'*64}))
        self.assertEqual(self.channel.update()['status'],'release-review-required');self.assertEqual(self.channel.engine.active(),old)
        other=self.base/'other.git';self.git(self.base,'init','--bare',other);self.git(self.repository,'remote','set-url','origin',other)
        self.assertEqual(self.channel.discover('codex','darwin-arm64','0.1.1')['status'],'release-review-required');self.assertEqual(self.channel.engine.active(),old)

    def test_policy_digest_and_platform_are_not_remote_grants(self):
        self.policy_path.write_text(json.dumps(dict(self.policy,capabilities=['all-data'])))
        with self.assertRaises(release.UpdateBlocked):release.ClientReleases(self.policy_path,self.policy_sha)
        report=self.channel.discover('codex','linux-aarch64','0.1.0');self.assertEqual(report['status'],'adapter-review-required')
        self.assertFalse((self.base/'never-read.token').exists())

    def test_ancestry_replacement_is_rejected_without_changing_active(self):
        self.publish();old=self.channel.update()['active']
        self.git(self.publisher,'checkout','--orphan','unrelated-code')
        self.git(self.publisher,'add','--',*release.CLIENT_FILES,'releases/manifest.json')
        self.git(self.publisher,'commit','-m','synthetic unrelated history')
        self.git(self.publisher,'push','origin','unrelated-code')
        unrelated=self.git(self.publisher,'rev-parse','HEAD')
        # Only this temporary bare fixture ref is replaced; never force-push.
        self.git(self.remote,'update-ref','refs/heads/releases/stable',unrelated)
        self.assertEqual(self.channel.update()['status'],'release-review-required');self.assertEqual(self.channel.engine.active(),old)


    def test_health_failure_restores_pointer_and_next_check_recovers(self):
        self.publish();old=self.channel.update()['active']
        self.publish('0.1.2',code=('connect.py',b'print("synthetic broken health")\n'))
        result=self.channel.update();self.assertEqual(result['status'],'release-review-required');self.assertEqual(result['active'],old)
        journal=json.loads(self.channel.engine.journal.read_text());self.assertEqual(journal['phase'],'rolled-back')
        self.publish('0.1.3');self.assertEqual(self.channel.update()['active']['version'],'0.1.3')

    def test_interrupted_activation_restores_then_retries(self):
        self.publish();old=self.channel.update()['active'];self.publish('0.1.2')
        with patch.object(self.channel.engine.adapter,'health',side_effect=KeyboardInterrupt('synthetic crash')):
            with self.assertRaises(KeyboardInterrupt):self.channel.update()
        self.assertEqual(json.loads(self.channel.engine.journal.read_text())['phase'],'activated')
        result=self.channel.update();self.assertEqual(result['status'],'updated');self.assertEqual(result['active']['version'],'0.1.2');self.assertNotEqual(result['active'],old)

    def test_interrupted_damaged_target_restores_verified_previous_package(self):
        self.publish();old=self.channel.update()['active'];self.publish('0.1.2')
        with patch.object(self.channel.engine.adapter,'health',side_effect=KeyboardInterrupt('synthetic crash')):
            with self.assertRaises(KeyboardInterrupt):self.channel.update()
        pending=json.loads(self.channel.engine.pointer.read_text())
        (Path(pending['package'])/'connect.py').write_text('damaged interrupted target')
        self.remote.rename(self.base/'offline-origin.git')
        result=self.channel.update()
        self.assertEqual(result['status'],'offline');self.assertEqual(result['active'],old)
        self.assertEqual(json.loads(self.channel.engine.journal.read_text())['phase'],'rolled-back-interrupted')

    def test_offline_startup_serves_only_previous_verified_client(self):
        self.publish();old=self.channel.update()['active'];self.remote.rename(self.base/'offline-origin.git')
        checked=client_startup.startup(self.policy_path,self.policy_sha,True);self.assertEqual(checked['status'],'offline');self.assertEqual(checked['active'],old)
        run=subprocess.run;commands=[]
        def serve_only(command,**kwargs):
            if any(str(arg).endswith('remote_mcp.py') for arg in command):
                commands.append(command);return subprocess.CompletedProcess(command,0)
            return run(command,**kwargs)
        with patch.object(client_startup.subprocess,'run',side_effect=serve_only):
            result=client_startup.startup(self.policy_path,self.policy_sha)
        self.assertTrue(result['served']);command=commands[0];self.assertIn(str(Path(old['package'])/'adapters/codex/remote_mcp.py'),command);self.assertEqual(command[-1],'darwin-arm64')
        target=Path(old['package'])/'connect.py';target.write_text('external package change')
        with patch.object(client_startup.subprocess,'run') as runner:
            result=client_startup.startup(self.policy_path,self.policy_sha)
        self.assertFalse(result.get('served',False));runner.assert_not_called()




class NativeConnection(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name);self.home=self.base/'home';self.home.mkdir()
        codex=self.home/'.codex';codex.mkdir();self.config=codex/'config.toml';self.config.write_text('model="fixture"\nsandbox_mode="read-only"\n[mcp_servers.other]\ncommand="fixture"\n')
        self.agents=codex/'AGENTS.md';self.agents.write_text('Other native instructions.\n');self.before={p:p.read_bytes() for p in (self.config,self.agents)}
        key=self.base/'fake-identity';key.write_text('not an SSH private key');key.chmod(0o600);known=self.base/'known-hosts';known.write_text('synthetic pinned host')
        self.profile=self.base/'profile.json';self.profile.write_text(json.dumps({'schema':1,'adapter':'codex','platform':'darwin-'+platform.machine().lower(),'data_mode':'synthetic-only','ssh':{'host':'fixture.invalid','user':'ubuntu','identity_file':str(key),'known_hosts_file':str(known),'known_hosts_sha256':hashlib.sha256(known.read_bytes()).hexdigest(),'remote_command':'/usr/bin/sudo -n -u agent-brain /usr/bin/python3 -I -B /opt/agent-brain-client/current/remote_mcp.py --endpoint http://127.0.0.1:8765 --token-file /var/lib/agent-brain/synthetic.token'}}));self.profile.chmod(0o600)
        self.platform=patch.object(sys,'platform','darwin');self.platform.start();self.addCleanup(self.platform.stop)
        self.environment=patch.dict(os.environ,{'CODEX_HOME':str(codex)});self.environment.start();self.addCleanup(self.environment.stop)
        self.transport=patch.object(native,'probe',return_value={'status':'not-configured','releases':[]});self.probe=self.transport.start();self.addCleanup(self.transport.stop)

    def register(self):
        report=remote_connect.connect(self.home,service_profile=self.profile)
        return remote_connect.connect(self.home,apply=True,service_profile=self.profile,plan_sha256=report['plan_sha256'])

    def test_single_checkpoint_plans_requires_digest_installs_and_reuses(self):
        self.assertEqual(remote_connect.connect(self.home)['status'],'server-authorization-required');self.probe.assert_not_called()
        report=remote_connect.connect(self.home,service_profile=self.profile);self.assertEqual(report['status'],'ready-to-register')
        blocked=remote_connect.connect(self.home,apply=True,service_profile=self.profile,plan_sha256='0'*64);self.assertEqual(blocked['status'],'plan-review-required')
        for p,raw in self.before.items():self.assertEqual(p.read_bytes(),raw)
        result=self.register();self.assertTrue(result['native_changed']);self.assertFalse(result['model_loaded'])
        self.assertIn('mcp_servers.other',self.config.read_text());self.assertTrue(self.agents.read_text().startswith(self.before[self.agents].decode()))
        after={p:p.read_bytes() for p in self.before};repeat=remote_connect.connect(self.home,apply=True);self.assertEqual(repeat['status'],'native-client-registered');self.assertFalse(repeat['native_changed'])
        for p,raw in after.items():self.assertEqual(p.read_bytes(),raw)
        self.assertFalse((self.home/'.codex/hooks.json').exists());self.assertFalse((self.home/'.agents').exists())

    def test_native_drift_and_changed_service_are_preserved(self):
        self.register();self.agents.write_text(self.agents.read_text()+native.END)
        before=self.agents.read_bytes();self.assertEqual(remote_connect.connect(self.home)['status'],'native-repair-review-required');self.assertEqual(self.agents.read_bytes(),before)
        self.agents.write_bytes(before[:-len(native.END)])
        value=json.loads(self.profile.read_text());value['ssh']['host']='different.invalid';self.profile.write_text(json.dumps(value))
        self.assertEqual(remote_connect.connect(self.home,service_profile=self.profile)['status'],'service-change-review-required')

    def test_shadowing_override_blocks_reuse_without_contacting_service(self):
        self.register();self.probe.reset_mock()
        override=self.home/'.codex/AGENTS.override.md';override.write_text('External instructions shadow the synthetic guard.')
        self.assertEqual(remote_connect.connect(self.home)['status'],'native-repair-review-required')
        self.probe.assert_not_called();self.assertEqual(override.read_text(),'External instructions shadow the synthetic guard.')

    def test_legacy_and_unknown_harness_are_review_steps_not_installations(self):
        legacy=self.home/'.config/agent-brain/device.json';legacy.parent.mkdir(parents=True);legacy.write_text('{"synthetic":"keep"}')
        self.assertEqual(remote_connect.connect(self.home,service_profile=self.profile)['status'],'migration-review-required');self.probe.assert_not_called()
        with patch.object(sys,'platform','win32'):
            self.assertEqual(remote_connect.connect(self.home,service_profile=self.profile)['status'],'adapter-review-required')
        self.assertEqual(legacy.read_text(),'{"synthetic":"keep"}')

    def test_failed_native_write_rolls_back_and_exact_plan_retries(self):
        report=remote_connect.connect(self.home,service_profile=self.profile);atomic=native.atomic
        def fail(path,raw,mode):
            if path.resolve()==self.agents.resolve():raise OSError('synthetic interrupted native write')
            return atomic(path,raw,mode)
        with patch.object(native,'atomic',side_effect=fail):
            with self.assertRaises(OSError):remote_connect.connect(self.home,apply=True,service_profile=self.profile,plan_sha256=report['plan_sha256'])
        for p,raw in self.before.items():self.assertEqual(p.read_bytes(),raw)
        self.assertTrue(self.register()['native_changed'])

    def test_unsafe_command_or_host_identity_drift_never_contacts_service(self):
        value=json.loads(self.profile.read_text());value['ssh']['remote_command']+='; echo unsafe';self.profile.write_text(json.dumps(value))
        with self.assertRaises(ValueError):remote_connect.connect(self.home,service_profile=self.profile)
        self.probe.assert_not_called()

    def test_known_profile_is_discovered_and_unknown_harness_reports_a_step(self):
        path=self.home/'.config/agent-brain-client/service-profile.json';path.parent.mkdir(parents=True);shutil.copyfile(self.profile,path);path.chmod(0o600)
        self.assertEqual(remote_connect.connect(self.home)['status'],'ready-to-register')
        run=subprocess.run([sys.executable,'-I','-B',str(ROOT/'connect.py'),'--agent','unknown-fixture'],capture_output=True,text=True)
        self.assertEqual(run.returncode,2);self.assertEqual(json.loads(run.stdout)['status'],'adapter-review-required')


if __name__=='__main__':unittest.main()
