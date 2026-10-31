"""Source build and existing install/update/task transactions, with fake calls."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import io,json,os,tempfile,unittest,sys
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'adapters/codex'),str(ROOT/'scripts')]
import agentbrain_040 as entry

class Management(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  for n in ('home','codex'):(self.root/n).mkdir()
  env=patch.dict(os.environ,{'HOME':str(self.root/'home'),'CODEX_HOME':str(self.root/'codex')},clear=True);env.start();self.addCleanup(env.stop)
 def run_entry(self,args):
  with patch('sys.stdout',new=io.StringIO()) as output:code=entry.main(args)
  return code,json.loads(output.getvalue())
 def test_real_source_build_is_unpublished_and_keeps_frozen_manifest(self):
  before=(ROOT/'releases/manifest.json').read_bytes();target=self.root/'candidate'
  code,value=self.run_entry(['build','--output',str(target)])
  self.assertEqual(code,0);self.assertFalse(value['published']);self.assertFalse(value['installed'])
  self.assertEqual(json.loads((target/'releases/manifest.json').read_text())['version'],'0.4.4')
  self.assertEqual(before,(ROOT/'releases/manifest.json').read_bytes())
 def test_apply_validates_then_loads_actual_plan_not_inspection_summary(self):
  plan={'schema':1,'fixture':'synthetic'};calls=[]
  with patch('content_install.inspect_plan',side_effect=lambda *a:calls.append('inspect') or {'status':'ready'}),patch('content_mcp.pinned',return_value=plan),patch('content_install.apply',side_effect=lambda p,d:calls.append(p) or {'installed':True}):
   code,value=self.run_entry(['apply','--plan-file','synthetic-plan','--plan-sha256','a'*64])
  self.assertEqual(code,0);self.assertEqual(calls,['inspect',plan])
 def test_drift_before_apply_never_runs_mutator(self):
  with patch('content_install.inspect_plan',side_effect=ValueError()),patch('content_install.apply') as apply:
   code,value=self.run_entry(['apply','--plan-file','synthetic-plan','--plan-sha256','a'*64])
  apply.assert_not_called();self.assertEqual(code,2);self.assertEqual(value['native_state'],'unconfirmed-review-required')
 def test_partial_apply_error_does_not_claim_rollback_or_unchanged_state(self):
  with patch('content_install.inspect_plan'),patch('content_mcp.pinned',return_value={}),patch('content_install.apply',side_effect=OSError()):
   code,value=self.run_entry(['apply','--plan-file','synthetic-plan','--plan-sha256','a'*64])
  self.assertEqual(code,2);self.assertNotIn('native_state_preserved',value)
 def test_update_uses_existing_manager_and_original_failure_exit(self):
  with patch('mac_first_install.manage_update',return_value={'status':'client-update-unavailable'}) as manager:
   code,value=self.run_entry(['update','--device-home',str(self.root/'home')])
  manager.assert_called_once_with((self.root/'home').resolve(),'update');self.assertEqual(code,2)
 def test_update_rejects_replacement_profile_before_manager(self):
  with patch('mac_first_install.manage_update') as manager,patch('sys.stderr',new=io.StringIO()):
   with self.assertRaises(SystemExit):entry.main(['update','--https-profile','unapproved'])
  manager.assert_not_called()
 def test_task_execution_delegates_once_and_restores_process_argv(self):
  previous=sys.argv
  with patch('https_content.main',return_value=2) as task:
   code,value=None,None
   with patch('sys.stdout',new=io.StringIO()):code=entry.main(['task-execute','--task-request-file','synthetic-request'])
  self.assertEqual(code,2);task.assert_called_once_with();self.assertIs(sys.argv,previous)

if __name__=='__main__':unittest.main()
