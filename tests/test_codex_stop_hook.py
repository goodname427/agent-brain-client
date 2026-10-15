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
        self.assertIn('no new user request/message',result['reason'])
        self.assertIn('request_id returned',result['reason'])
        self.assertIn('Do not read transcript files',result['reason'])
        self.assertIn('shared across projects',result['reason'])
        self.assertIn('ordinary project experiences',result['reason'])
        self.assertIn('cannot be safely generalized',result['reason'])
        self.assertIn('Ignore assistant guesses',result['reason'])
        self.assertIn('durable Server result',result['reason'])
        self.assertRegex(result['reason'],r'Stop turn_sha256: [a-f0-9]{64}')

    def test_same_turn_reuses_stable_request_id(self):
        first=codex_stop_hook(self.event())['reason']
        repeated=codex_stop_hook(self.event())['reason']
        self.assertEqual(first,repeated)
        other=codex_stop_hook(self.event(turn_id='another-synthetic-turn'))['reason']
        self.assertNotEqual(first,other)

    def test_request_id_is_stable_per_operation_and_distinct_per_target(self):
        from content_mcp import ContentServer
        class Adapter:
            def methods(self):return set()
            policy={'kinds':['memory']}
        server=ContentServer(Adapter())
        args={'turn_sha256':'a'*64,'method':'content_put','kind':'memory','id':'synthetic-a'}
        first=server.invoke('content_request_id',args)['structuredContent']['request_id']
        self.assertEqual(first,server.invoke('content_request_id',args)['structuredContent']['request_id'])
        other=server.invoke('content_request_id',dict(args,id='synthetic-b'))['structuredContent']['request_id']
        self.assertNotEqual(first,other)

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
