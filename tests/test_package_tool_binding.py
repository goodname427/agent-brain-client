"""Frozen 0.6.2 tool contract and synthetic future contract; no credentials."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

import content_install as ci
import content_mcp
import mac_first_install as wizard
import test_content_install as fixture

FROZEN=ROOT/'tests/fixtures/tool_contract_062/content_mcp.py.fixture'


def old_contract(manifest, output):
    raw=FROZEN.read_bytes()
    if ci.sha(raw)!='506fd93821989460b5243c0bb7a32821503630f5f8b9d582ec2cc218655cc530':
        raise ValueError('Frozen 0.6.2 contract fixture changed')
    (output/'adapters/codex/content_mcp.py').write_bytes(raw)
    manifest['files']['adapters/codex/content_mcp.py']=ci.sha(raw)


@unittest.skipIf(sys.platform=='win32','Portable Mac install fixture; package contract tests run on both platforms')
class BindingMigration(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ContentInstall('test_prepare_read_only_then_apply_inspect_reuse_and_single_link_state')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.publish('0.6.2',mutate=old_contract)
        self.f.package=Path(self.f.channel.update()['active']['package'])
        plan=self.f.plan()
        self.assertEqual(len(plan['mcp']['enabled_tools']),9)
        self.assertTrue(ci.apply(plan,self.f.digest(plan),lambda p:None)['installed'])
        self.old_package=self.f.package
        self.old_metadata=ci.private_json(self.f.home/'.config/agent-brain-client/codex-mcp.json')
        self.f.publish('0.6.4')
        self.before=self.f.channel.update()['active']

    def test_old_nine_tool_binding_is_authenticated_readonly_then_migrated(self):
        files=self.f.native_files()
        with patch.object(ci,'package_tools',side_effect=lambda package,policy,grant:content_mcp.enabled_tools(policy,grant)):
            self.assertEqual(ci.inspect(self.f.home)['reason'],'mcp-binding-changed')
        self.assertTrue(ci.inspect(self.f.home)['registration_verified'])
        self.assertEqual(self.f.native_files(),files)
        profile=self.f.profile.read_bytes();policy=self.f.policy_path.read_bytes()
        factory=lambda *a:self.f.channel
        with patch.object(wizard,'HTTPS_CONTENT_CAPABILITIES',self.f.policy['capabilities']):
            checked=wizard.manage_update(self.f.home,'check-update',channel_factory=factory)
            self.assertEqual(checked['status'],'client-update-available',checked)
            self.assertEqual(checked['registered_tool_count'],9)
            self.assertEqual(checked['tool_discovery']['blocked_by_registration'],['memory_recall'])
            self.assertEqual(self.f.native_files(),files)
            result=wizard.manage_update(self.f.home,'update',channel_factory=factory)
        self.assertEqual(result['status'],'client-update-current',result)
        self.assertTrue(result['native_changed']);self.assertTrue(result['reload_required'])
        table=ci.tomllib.loads(self.f.config.read_text())['mcp_servers'][ci.NAME]
        self.assertIn('memory_recall',table['enabled_tools']);self.assertEqual(len(table['enabled_tools']),10)
        self.assertTrue(ci.inspect(self.f.home)['registration_verified'])
        metadata=ci.private_json(self.f.home/'.config/agent-brain-client/codex-mcp.json')
        self.assertNotEqual(metadata['package'],self.old_metadata['package'])
        self.assertEqual(metadata['release_policy_sha256'],self.old_metadata['release_policy_sha256'])
        self.assertEqual(self.f.profile.read_bytes(),profile);self.assertEqual(self.f.policy_path.read_bytes(),policy)
        self.assertEqual(self.f.channel.verified_active(),self.before)
        self.assertIsNone(result['running_worker_version'])
        self.assertEqual(ci.tomllib.loads(self.f.config.read_text())['mcp_servers']['other'],{'command':'preserve-other'})

    def test_tool_command_and_env_drift_still_fail_without_rewriting(self):
        original=self.f.config.read_bytes()
        for field,value in (('enabled_tools',['content_read']),('command',str(self.f.component)),('env',{'SYNTHETIC':'changed'})):
            parsed=ci.tomllib.loads(original.decode());parsed['mcp_servers'][ci.NAME][field]=value
            self.f.config.write_bytes(ci.render(original,parsed['mcp_servers'][ci.NAME],True))
            before=self.f.config.read_bytes()
            with self.subTest(field=field):
                self.assertEqual(ci.inspect(self.f.home)['reason'],'mcp-binding-changed')
                self.assertEqual(self.f.config.read_bytes(),before)
        self.f.config.write_bytes(original)

    def test_changed_frozen_package_is_rejected_before_tool_probe(self):
        file=self.old_package/'adapters/codex/content_mcp.py'
        file.write_bytes(file.read_bytes()+b'\n# Synthetic unverified package mutation\n')
        config=self.f.config.read_bytes()
        with patch.object(ci,'package_tools',side_effect=AssertionError('Unverified package must not run')):
            self.assertFalse(ci.inspect(self.f.home).get('registration_verified',False))
        self.assertEqual(self.f.config.read_bytes(),config)


@unittest.skipIf(sys.platform=='win32','Portable Mac install fixture; contract tests run on both platforms')
class LegacyFilterMigration(unittest.TestCase):
    def setUp(self):
        from pending_install_recovery import legacy_plan
        self.f=fixture.ContentInstall('test_prepare_read_only_then_apply_inspect_reuse_and_single_link_state')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        plan=legacy_plan(self.f.plan())
        with patch.object(ci,'prepare',return_value=plan):
            self.assertTrue(ci.apply(plan,self.f.digest(plan),lambda p:None)['installed'])

    def test_current_package_repairs_legacy_filter_without_replaying_queue(self):
        # A current active/launcher package used to skip replacement entirely.
        # Its authenticated three-tool host filter therefore survived every
        # update/restart, although tools/list advertised the newer contract.
        config=self.f.config.read_bytes()
        profile=self.f.profile.read_bytes();policy=self.f.policy_path.read_bytes()
        cache=Path(self.f.policy['runtime']['content_cache'])
        outbox=cache/'outbox';outbox.mkdir(parents=True,exist_ok=True)
        pending=outbox/'synthetic-pending.json'
        pending.write_bytes(b'{"synthetic":"pending-preserve-exactly"}')
        factory=lambda *a:self.f.channel
        with patch.object(wizard,'HTTPS_CONTENT_CAPABILITIES',self.f.policy['capabilities']):
            checked=wizard.manage_update(self.f.home,'check-update',channel_factory=factory)
            self.assertEqual(self.f.config.read_bytes(),config)
            self.assertEqual(checked['status'],'client-update-available',checked)
            self.assertIn('memory_recall',checked['tool_discovery']['blocked_by_registration'])
            self.assertTrue(checked['tool_discovery']['registration_update_required'])
            self.assertEqual(checked['tool_discovery']['model_tool_visibility'],'unknown')
            updated=wizard.manage_update(self.f.home,'update',channel_factory=factory)
        self.assertEqual(updated['status'],'client-update-current',updated)
        self.assertTrue(updated['native_changed']);self.assertTrue(updated['reload_required'])
        self.assertEqual(updated['tool_discovery']['blocked_by_registration'],[])
        self.assertFalse(updated['tool_discovery']['registration_update_required'])
        self.assertEqual(updated['tool_discovery']['host_tool_discovery'],'unknown')
        table=ci.tomllib.loads(self.f.config.read_text())['mcp_servers'][ci.NAME]
        self.assertIn('memory_recall',table['enabled_tools'])
        self.assertEqual(len(table['enabled_tools']),10)
        self.assertEqual(self.f.profile.read_bytes(),profile)
        self.assertEqual(self.f.policy_path.read_bytes(),policy)
        self.assertEqual(pending.read_bytes(),b'{"synthetic":"pending-preserve-exactly"}')
        self.assertTrue(ci.inspect(self.f.home)['registration_verified'])

    def test_paused_filter_repair_preserves_pause_and_rejects_external_edits(self):
        paused=ci.tomllib.loads(self.f.config.read_text())['mcp_servers'][ci.NAME]
        paused['enabled']=False
        self.f.config.write_bytes(ci.render(self.f.config.read_bytes(),paused,True))
        plan=self.f.plan(replace=True)
        self.assertFalse(plan['mcp']['enabled'])
        self.assertIn('memory_recall',plan['mcp']['enabled_tools'])
        before=self.f.config.read_bytes()
        self.f.config.write_bytes(before+b'\n# synthetic external edit\n')
        edited=self.f.config.read_bytes()
        with self.assertRaises(ci.InstallBlocked):
            ci.apply(plan,self.f.digest(plan),lambda p:None)
        self.assertEqual(self.f.config.read_bytes(),edited)
        plan=self.f.plan(replace=True)
        self.assertTrue(ci.apply(plan,self.f.digest(plan),lambda p:None)['installed'])
        self.assertEqual(ci.inspect(self.f.home)['installation_state'],'verified-disabled')


if __name__=='__main__':unittest.main()
