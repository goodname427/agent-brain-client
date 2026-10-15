"""Synthetic tests for the documented Codex notify payload; no live turn is queued."""
import json
import subprocess
import sys
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'adapters/codex'))
from content_mcp import codex_notify_capture,codex_notify_main,_NOTIFY_CAPTURE_MARKER


class CodexNotify(unittest.TestCase):
    def event(self,**changes):
        value={'type':'agent-turn-complete','thread-id':'synthetic-thread','turn-id':'synthetic-turn',
               'input-messages':['Synthetic user stated progress.'],'last-assistant-message':'Synthetic acknowledgement.'}
        value.update(changes)
        return value

    def test_documented_payload_queues_one_capture_to_existing_thread(self):
        event=self.event();capture=codex_notify_capture(event)
        self.assertEqual(capture['thread_id'],'synthetic-thread')
        self.assertEqual(capture['turn_sha256'],__import__('hashlib').sha256(b'synthetic-turn').hexdigest())
        self.assertIn(_NOTIFY_CAPTURE_MARKER+'synthetic-turn',capture['message'])
        self.assertEqual(capture['input_message_count'],1)
        launch=Mock(return_value=SimpleNamespace(returncode=0));self.assertEqual(codex_notify_main([json.dumps(event)],launch),0)
        args,kwargs=launch.call_args
        self.assertEqual(args[0][1:5],['queue','--thread','synthetic-thread','--message'])
        self.assertIn(_NOTIFY_CAPTURE_MARKER+'synthetic-turn',args[0][5])
        self.assertNotIn('Synthetic user stated progress.',args[0][5])
        self.assertEqual(kwargs['stdin'],subprocess.DEVNULL)
        self.assertEqual(kwargs['timeout'],8)

    def test_queue_failure_retries_exact_request_then_surfaces_failure(self):
        event=self.event();launch=Mock(return_value=SimpleNamespace(returncode=1))
        self.assertEqual(codex_notify_main([json.dumps(event)],launch),1)
        self.assertEqual(launch.call_count,2)
        self.assertEqual(launch.call_args_list[0],launch.call_args_list[1])

    def test_queue_transient_failure_retries_and_succeeds(self):
        event=self.event();launch=Mock(side_effect=[subprocess.TimeoutExpired('codex queue',8),SimpleNamespace(returncode=0)])
        self.assertEqual(codex_notify_main([json.dumps(event)],launch),0)
        self.assertEqual(launch.call_count,2)

    def test_empty_or_internal_messages_do_not_start_a_capture(self):
        self.assertIsNone(codex_notify_capture(self.event(type='approval-requested')))
        self.assertIsNone(codex_notify_capture(self.event(**{'input-messages':[]})))
        self.assertIsNone(codex_notify_capture(self.event(**{'input-messages':[_NOTIFY_CAPTURE_MARKER+'previous-turn']})))
        self.assertIsNone(codex_notify_capture(self.event(**{'input-messages':[None]})))

    def test_cli_reads_one_json_argument_and_rejects_invalid_or_oversized_payload(self):
        launch=Mock()
        self.assertEqual(codex_notify_main(['{bad'],launch),0)
        self.assertEqual(codex_notify_main([' '*1048577],launch),0)
        self.assertEqual(codex_notify_main([],launch),0)
        launch.assert_not_called()

    def test_script_flag_runs_before_mcp_configuration_is_required(self):
        import content_mcp
        event=self.event();launch=Mock(return_value=SimpleNamespace(returncode=0))
        with patch.object(content_mcp.sys,'argv',['content_mcp.py','--codex-notify',json.dumps(event)]), \
             patch.object(content_mcp.subprocess,'run',launch):
            with self.assertRaises(SystemExit) as result:content_mcp.main()
        self.assertEqual(result.exception.code,0)
        launch.assert_called_once()


if __name__=='__main__':unittest.main()
