import agent_brain_client.application.control as application_control
"""Shared memory code works without loading a Codex adapter or real identity."""
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

import agent_brain_client.domain.contract as contract
import agent_brain_client.adapters.server.http as brain_http
from agent_brain_client.application.queue.outbox import DurableOutbox
from agent_brain_client.domain.errors import CacheBlocked
from agent_brain_client.application.memory.capture import submit_turn
from agent_brain_client.domain.turns import normalized_turn

ROOT = Path(__file__).resolve().parents[1]


class MemoryHost(DurableOutbox):
    """Fictional host supplies storage and policy, never a native config mapper."""
    _outbox_lock = staticmethod(lambda *args: nullcontext())
    _outbox_stage = staticmethod(lambda *args: nullcontext())

    def __init__(self, root, rows=None):
        self.cache = root
        self.rows = {} if rows is None else rows
        self.sources = self
        self.policy = {'kinds': ['memory']}
        self.queue_sha = 'a' * 64
        self.sent = []

    def _load(self, name, default=None):
        return json.loads(self.rows[name]) if name in self.rows else default

    def _save(self, name, value):
        self.rows[name] = json.dumps(value)

    def prepare_capture_stage(self, *, limit):
        pass

    def _safe(self, path, base):
        self.cache.mkdir(exist_ok=True)

    def methods(self):
        return {'memory_event_submit'}

    def _allows(self, kind, identity):
        return kind == 'memory'

    def assert_shareable(self, keys):
        if keys:
            raise AssertionError('No native-source reconciliation in this fixture')

    def validate_reconciliation(self, source_keys, kind, identity, record):
        self.assert_shareable(source_keys)

    def confirm_reconciliation(self, source_keys, request_id):
        self.assert_shareable(source_keys)

    def block_reconciliation(self, source_keys, request_id):
        self.assert_shareable(source_keys)

    def _queued(self):
        return [json.loads(raw) for name, raw in self.rows.items() if name.startswith('outbox/')]

    def invoke(self, request, *, event_gate):
        if event_gate():
            self.sent.append(request)
        return {'request_id': request['request_id'], 'sent': bool(self.sent)}


class HarnessCore(unittest.TestCase):
    def test_shared_imports_do_not_load_codex_or_discover_a_host(self):
        code = '''import sys
sys.path.insert(0,sys.argv[1])
from agent_brain_client.domain import contract
from agent_brain_client.application import control
from agent_brain_client.application.queue import outbox
assert not any(n in sys.modules for n in ('content_cache','content_mcp','codex_connect','client_release','https_content','brain_core'))
'''
        run = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(ROOT / 'src')], capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr.decode())

    def test_native_and_transport_consumers_share_contract_and_request_cancellation(self):
        import agent_brain_client.adapters.harnesses.codex.content as content_cache
        from agent_brain_client.application.control import RequestControl as MCPControl
        from agent_brain_client.adapters.server.https import RequestStopped as TransportStopped
        self.assertIs(content_cache.contract, contract)
        self.assertIs(MCPControl, application_control.RequestControl)
        self.assertIs(TransportStopped, application_control.RequestStopped)
        control = MCPControl(30)
        try:
            with control.activate():
                self.assertIs(application_control.current_request(), control)
        finally:
            control.close()

    def test_http_adapter_identity_is_explicit_and_defaults_to_codex(self):
        for adapter in ('codex', 'codebuddy'):
            client = brain_http.RemoteClient('https://synthetic.invalid', credential_provider=lambda: 'a' * 32, adapter=adapter)
            requests = []

            def opened(request, **kwargs):
                requests.append(request)
                response = io.BytesIO(json.dumps({'request_id': 'synthetic_request_1234', 'result': {}}).encode())
                response.status = 200
                return response

            client.opener = SimpleNamespace(open=opened)
            client.request({'request_id': 'synthetic_request_1234', 'method': 'content_catalog', 'params': {'kinds': ['memory']}})
            self.assertEqual(requests[0].get_header('X-agent-brain-adapter'), adapter)
        self.assertEqual(brain_http.RemoteClient('https://synthetic.invalid', credential_provider=lambda: 'a' * 32).adapter, 'codex')

    def test_normalized_turn_restart_exact_queue_replay_and_stop_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            host = MemoryHost(Path(tmp))
            scope = {'cwd': str(Path(tmp)), 'project_id': 'fictional-project', 'session_id': 'fictional-session'}
            key = normalized_turn(scope, 'fictional-turn', 'prompt', 'Fictional prompt')
            self.assertEqual(key, hashlib.sha256(contract.encode([scope, 'fictional-turn'])).hexdigest())
            submit_turn(host, scope, 'fictional-turn', 'prompt', 'Fictional prompt', enabled=lambda: True, lock=lambda _: nullcontext(), clock=lambda: datetime(2026, 11, 5, tzinfo=timezone.utc))
            restarted = MemoryHost(Path(tmp), host.rows)
            result = submit_turn(restarted, scope, 'fictional-turn', 'final', 'Fictional answer', enabled=lambda: True, lock=lambda _: nullcontext())
            request = restarted.sent[0]
            self.assertEqual(result['request_id'], 'agentbrain_event_' + key)
            self.assertEqual(request['params']['id'], 'evt-' + key[:56])
            contract.validate(request['method'], request['params'])
            saved = restarted.rows['outbox/' + request['request_id'] + '.json']
            self.assertEqual(restarted._enqueue(request)['request'], request)
            self.assertEqual(restarted.rows['outbox/' + request['request_id'] + '.json'], saved)
            changed = json.loads(contract.encode(request))
            changed['params']['event']['messages'][0]['content'] = 'Changed prompt'
            with self.assertRaises(CacheBlocked):
                restarted._enqueue(changed)
            disabled = submit_turn(restarted, scope, 'fictional-turn', 'final', 'Fictional answer', enabled=lambda: False, lock=lambda _: nullcontext())
            self.assertEqual(disabled['status'], 'disabled')
            self.assertEqual(len(restarted.sent), 1)
