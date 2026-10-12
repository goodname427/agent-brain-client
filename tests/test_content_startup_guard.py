"""Fixed bootstrap refuses a legacy runtime while preserving local evidence."""
import copy
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'adapters/codex'),str(ROOT/'scripts')]
import client_startup as startup
from client_release import SSH_CONTENT_CAPABILITIES,CONTENT_CAPABILITIES,CAPABILITIES


class StartupGuard(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.base=Path(tmp.name)
        self.cache=self.base/'content-cache';self.cache.mkdir(mode=0o700)
        self.policy={'capabilities':SSH_CONTENT_CAPABILITIES,'runtime':{'content_cache':str(self.cache),'device_home':str(self.base/'home')}}
        self.active={'version':'0.2.0','commit':'a'*40,'package':str(self.base/'old-package')}
        self.status='offline'

    def call(self,**options):
        result={'status':self.status,'active':copy.deepcopy(self.active)}
        fixed=SimpleNamespace(policy=self.policy,update=lambda:result,verified_active=lambda:self.active)
        with patch.object(startup,'ClientReleases',return_value=fixed),patch.object(startup.subprocess,'run') as serve,patch('content_install.inspect',return_value={'installed':True}):
            serve.return_value=SimpleNamespace(returncode=0)
            value=startup.startup('unused-policy','a'*64,**options)
            return value,serve.call_count

    def test_offline_or_failed_health_fallback_never_serves_legacy_with_pending(self):
        outbox=self.cache/'outbox';outbox.mkdir();pending=outbox/'synthetic-pending.json';pending.write_bytes(b'Synthetic retained request and CAS')
        before=pending.read_bytes()
        for status in ('offline','release-review-required','current'):
            self.status=status
            for options in ({},{'check_only':True},{'installation_check':True}):
                result,calls=self.call(**options)
                self.assertEqual(result['status'],'content-outbox-runtime-incompatible');self.assertFalse(result['served']);self.assertEqual(calls,0)
                self.assertEqual(result['active'],self.active);self.assertEqual(result['required_client_version'],'0.2.1');self.assertIn('do not delete',result['next_action']);self.assertEqual(pending.read_bytes(),before)

    def test_import_inventory_alone_blocks_both_content_transports(self):
        inventory=self.cache/'imports.json';inventory.write_bytes(b'Synthetic retained inventory')
        for capabilities in (SSH_CONTENT_CAPABILITIES,CONTENT_CAPABILITIES):
            self.policy['capabilities']=capabilities
            result,calls=self.call();self.assertEqual(result['status'],'content-outbox-runtime-incompatible');self.assertEqual(calls,0)
        self.assertEqual(inventory.read_bytes(),b'Synthetic retained inventory')

    def test_empty_or_linked_evidence_is_retained_without_reading_targets(self):
        outbox=self.cache/'outbox';outbox.mkdir()
        self.assertEqual(self.call()[0]['status'],'content-outbox-runtime-incompatible')
        outbox.rmdir();outbox.symlink_to(self.base/'absent-target')
        self.assertEqual(self.call()[0]['status'],'content-outbox-runtime-incompatible');self.assertTrue(outbox.is_symlink())

    def test_unreadable_state_blocks_legacy_with_recovery_reason(self):
        original=Path.lstat
        def fail(path,*args,**kwargs):
            if path==self.cache/'outbox':raise PermissionError('Synthetic state inaccessible')
            return original(path,*args,**kwargs)
        with patch.object(Path,'lstat',fail):result,calls=self.call()
        self.assertEqual(result['status'],'content-local-state-unverified');self.assertEqual(calls,0);self.assertFalse(result['native_changed']);self.assertIn('restore access',result['next_action'])

    def test_compatible_runtime_resumes_with_original_ledger_untouched(self):
        outbox=self.cache/'outbox';outbox.mkdir();pending=outbox/'synthetic-pending.json';pending.write_bytes(b'Synthetic pending')
        result,calls=self.call();self.assertFalse(result['served']);self.assertEqual(calls,0)
        self.active['version']='0.2.1';self.status='updated'
        result,calls=self.call();self.assertTrue(result['served']);self.assertEqual(calls,1);self.assertEqual(pending.read_bytes(),b'Synthetic pending')

    def test_legacy_without_ledgers_and_noncontent_runtime_remain_usable(self):
        result,calls=self.call();self.assertTrue(result['served']);self.assertEqual(calls,1)
        (self.cache/'outbox').mkdir();self.policy={'platform':'darwin-arm64','capabilities':CAPABILITIES,'runtime':{'endpoint':'http://127.0.0.1:1','token_file':str(self.base/'unread-token')}}
        result,calls=self.call();self.assertTrue(result['served']);self.assertEqual(calls,1);self.assertFalse((self.base/'unread-token').exists())

    def test_check_only_cli_reports_explicit_block_and_exits_nonzero(self):
        (self.cache/'imports.json').write_bytes(b'Synthetic inventory')
        result,calls=self.call(check_only=True);self.assertEqual(calls,0)
        args=['startup','--policy-file','unused','--policy-sha256','a'*64,'--check-only']
        with patch.object(sys,'argv',args),patch.object(startup,'startup',return_value=result),patch.object(sys,'stdout',io.StringIO()) as output:
            self.assertEqual(startup.main(),2);self.assertIn('content-outbox-runtime-incompatible',output.getvalue())

    def test_ledger_created_after_initial_check_is_rechecked_under_lease(self):
        original=startup.content_runtime_blocker;checks=[]
        inventory=self.cache/'imports.json'
        def interleaved(active,policy):
            checks.append(True)
            if len(checks)==1:
                self.assertIsNone(original(active,policy))
                inventory.write_bytes(b'SYNTHETIC_LATE_INVENTORY')
                return None
            return original(active,policy)
        with patch.object(startup,'content_runtime_blocker',side_effect=interleaved):result,calls=self.call()
        self.assertEqual(result['status'],'content-outbox-runtime-incompatible');self.assertEqual(calls,0)
        self.assertEqual(len(checks),2);self.assertEqual(inventory.read_bytes(),b'SYNTHETIC_LATE_INVENTORY')

    def test_linked_lease_is_preserved_and_readonly_checks_create_no_lease(self):
        lease=self.cache/'.content-runtime.lock'
        for options in ({'check_only':True},{'installation_check':True}):
            self.call(**options);self.assertFalse(lease.exists())
        lease.symlink_to(self.base/'unread-external-target')
        result,calls=self.call();self.assertEqual(result['status'],'content-runtime-lifecycle-review-required')
        self.assertEqual(calls,0);self.assertTrue(lease.is_symlink());self.assertFalse(lease.resolve().exists())

    def test_launch_failure_releases_kernel_lease_for_next_start(self):
        with patch.object(startup.subprocess,'run',side_effect=OSError('Synthetic launch failed')), patch('content_install.inspect',return_value={'installed':True}):
            result=startup.serve_content(['synthetic-unused'],self.active,self.policy,'offline')
        self.assertEqual(result['status'],'content-runtime-lifecycle-review-required')
        result,calls=self.call();self.assertTrue(result['served']);self.assertEqual(calls,1)

    def test_removed_registration_after_initial_inspect_cannot_launch_worker(self):
        self.active['version']='0.2.5'
        fixed=SimpleNamespace(policy=self.policy,update=lambda:{'status':'current','active':self.active},verified_active=lambda:self.active)
        with patch.object(startup,'ClientReleases',return_value=fixed),patch.object(startup.subprocess,'run') as serve,patch('content_install.inspect',side_effect=[{'installed':True},{'installed':False}]):
            result=startup.startup('unused-policy','a'*64)
        self.assertEqual(result['status'],'content-native-install-pending');self.assertFalse(result['served']);serve.assert_not_called()

if __name__=='__main__':unittest.main()
