"""Synthetic tests for the documented Codex notify payload; no live turn is queued."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import agent_brain_client.interfaces.mcp.content as content_mcp
from agent_brain_client.interfaces.mcp.content import codex_notify_capture, _NOTIFY_CAPTURE_MARKER


class CodexNotify(unittest.TestCase):
    def event(self,**changes):
        value={'type':'agent-turn-complete','thread-id':'synthetic-thread','turn-id':'synthetic-turn',
               'input-messages':['Synthetic user stated progress.'],'last-assistant-message':'Synthetic acknowledgement.'}
        value.update(changes)
        return value



    def test_empty_or_internal_messages_do_not_start_a_capture(self):
        self.assertIsNone(codex_notify_capture(self.event(type='approval-requested')))
        self.assertIsNone(codex_notify_capture(self.event(**{'input-messages':[]})))
        self.assertIsNone(codex_notify_capture(self.event(**{'input-messages':[_NOTIFY_CAPTURE_MARKER+'previous-turn']})))
        self.assertIsNone(codex_notify_capture(self.event(**{'input-messages':[None]})))




if __name__=='__main__':unittest.main()
