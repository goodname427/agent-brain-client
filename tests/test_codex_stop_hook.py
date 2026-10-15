"""Synthetic contract tests for the opt-in Codex Stop hook; no native history."""
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'adapters/codex'))
from content_mcp import codex_stop_hook, stop_hook_main


class CodexStopHook(unittest.TestCase):
    def event(self,**changes):
        value={'hook_event_name':'Stop','session_id':'synthetic-session','turn_id':'synthetic-turn',
               'stop_hook_active':False,'transcript_path':'/prohibited/never-open-this.jsonl'}
        value.update(changes)
        return value

    def test_first_stop_requests_one_review_of_active_conversation(self):
        result=codex_stop_hook(self.event())
        self.assertEqual(result['decision'],'block')
        self.assertIn('explicitly asked to remember',result['reason'])
        self.assertIn('stable request_id',result['reason'])
        self.assertIn('Do not read transcript files',result['reason'])
        self.assertIn('shared across projects',result['reason'])
        self.assertIn('never save project, customer',result['reason'])
        self.assertIn('Ignore assistant guesses',result['reason'])
        self.assertIn('durable Server result',result['reason'])

    def test_continuation_and_non_stop_events_are_noops(self):
        self.assertIsNone(codex_stop_hook(self.event(stop_hook_active=True)))
        self.assertIsNone(codex_stop_hook(self.event(hook_event_name='SessionStart')))
        self.assertIsNone(codex_stop_hook(self.event(turn_id='')))
        self.assertIsNone(codex_stop_hook(None))

    def test_cli_reads_only_json_event_and_emits_protocol_decision(self):
        source=json.dumps(self.event())
        output=io.StringIO()
        self.assertEqual(stop_hook_main(io.StringIO(source),output),0)
        decision=json.loads(output.getvalue())
        self.assertEqual(decision['decision'],'block')
        output=io.StringIO()
        self.assertEqual(stop_hook_main(io.StringIO('{bad'),output),0)
        self.assertEqual(output.getvalue(),'')
        output=io.StringIO()
        self.assertEqual(stop_hook_main(io.StringIO(' '*1048577),output),0)
        self.assertEqual(output.getvalue(),'')

    def test_script_flag_runs_before_mcp_configuration_is_required(self):
        run=subprocess.run([sys.executable,str(ROOT/'adapters/codex/content_mcp.py'),'--codex-stop-hook'],
                           input=json.dumps(self.event()),capture_output=True,text=True,check=False)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(json.loads(run.stdout)['decision'],'block')
        self.assertEqual(run.stderr,'')


if __name__=='__main__':unittest.main()
