"""Synthetic MCP clients only; no real tokens, Harness configuration or models."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('remote_mcp', ROOT / 'adapters/codex/remote_mcp.py')
bridge = importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)


class Client:
    def __init__(self):
        self.requests = []
        self.error = False

    def request(self, value):
        self.requests.append(value)
        if self.error:
            return {'request_id': value['request_id'], 'error': {'code': 'persistence_pending', 'retryable': True},
                    'persistence': {'durable': False}}
        return {'request_id': value['request_id'], 'result': {'revision': 'synthetic-revision'},
                'persistence': {'durable': True}}


class RemoteMCP(unittest.TestCase):
    def setUp(self):
        self.client = Client();self.server = bridge.Server(self.client)

    def message(self, method, params=None, identifier=1):
        return {'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params or {}}

    def ready(self):
        result = self.server.handle(self.message('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                   'clientInfo': {'name': 'synthetic-codex', 'version': '1'}}))
        self.server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        return result

    def test_lifecycle_and_minimal_discovery_no_core_capabilities(self):
        with self.assertRaises(bridge.ProtocolError):self.server.handle(self.message('tools/list'))
        result = self.ready();self.assertEqual(result['protocolVersion'], '2025-06-18')
        tools = self.server.handle(self.message('tools/list'))['tools']
        self.assertEqual({v['name'] for v in tools}, {'topic', 'recall', 'remember', 'forget', 'client_updates'})
        for tool in tools:
            self.assertFalse(tool['inputSchema']['additionalProperties'])
            if tool['name'] in bridge.WRITES:
                self.assertIn('request_id', tool['inputSchema']['required'])
                self.assertIn('expected_revision', tool['inputSchema']['required'])
                self.assertFalse(tool['annotations']['readOnlyHint'])
        self.assertEqual(self.client.requests, [])

    def test_daily_instructions_require_exact_authenticated_server_guidance(self):
        policy={'write_mode':'daily-personal','scope':'personal-shared','allowed_write_methods':['remember'],
                'proactive_maintenance':True,'raw_history_collection':False}
        responses=[{'result':{'memory_policy':policy}},
                   {'result':{'memory_policy':dict(policy,allowed_write_methods=['remember','forget'])}},
                   {'result':{'memory_policy':dict(policy,raw_history_collection=True)}},
                   {'error':{'code':'permission_denied'}}, {'result':{}}]
        for i,value in enumerate(responses):
            seen=[]
            class GuidedClient:
                def request(self,req,*,timeout=90,deadline=None):seen.append(req);return value
            self.server=bridge.Server(GuidedClient(),data_mode='personal');result=self.ready()
            self.assertEqual(self.server.daily,i==0)
            if i==0:
                self.assertEqual(result['instructions'],bridge.DAILY_INSTRUCTIONS)
                tools=self.server.handle(self.message('tools/list'))['tools'];forget=next(t for t in tools if t['name']=='forget')
                self.assertIn('disabled',forget['description'])
            else:self.assertIn('No automatic collection',result['instructions'])
            self.assertEqual([r['method'] for r in seen],['client_updates'])
            self.assertEqual(seen[0]['params']['adapter'],'codex')

    def test_stalled_policy_probe_keeps_tools_available_with_short_http_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            token=Path(temporary)/'synthetic.token';token.write_text('SYNTHETIC_ONLY_TOKEN_FIXTURE_1234567890');token.chmod(0o600)
            client=bridge.RemoteClient('http://127.0.0.1:8765',token,'personal');timeouts=[]
            class StalledEndpoint:
                def open(self,request,timeout):timeouts.append(timeout);raise TimeoutError('SYNTHETIC_STALLED_ENDPOINT')
            client.opener=StalledEndpoint();self.server=bridge.Server(client,data_mode='personal')
            result=self.ready();self.assertEqual(timeouts,[8]);self.assertFalse(self.server.daily)
            self.assertIn('No automatic collection',result['instructions'])
            self.assertEqual(len(self.server.handle(self.message('tools/list'))['tools']),5)
            value=self.server.handle(self.message('tools/call',{'name':'topic','arguments':{'id':'synthetic'}}))
            self.assertTrue(value['isError']);self.assertEqual(timeouts,[8,90])
            self.assertNotIn('SYNTHETIC_STALLED_ENDPOINT',json.dumps(value))

    def test_slow_drip_http_policy_has_total_deadline_and_cannot_start_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            token=Path(temporary)/'synthetic.token';token.write_text('SYNTHETIC_ONLY_TOKEN_FIXTURE_1234567890');token.chmod(0o600)
            class SlowResponse(BaseHTTPRequestHandler):
                def do_POST(self):
                    req=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    data=json.dumps({'request_id':req['request_id'],'result':{'memory_policy':{}}}).encode()
                    self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers()
                    try:
                        for byte in data:self.wfile.write(bytes([byte]));self.wfile.flush();time.sleep(.015)
                    except OSError:pass
                def log_message(self,*args):pass
            http=ThreadingHTTPServer(('127.0.0.1',0),SlowResponse);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            try:
                client=bridge.RemoteClient('http://127.0.0.1:'+str(http.server_port),token,'personal')
                req={'request_id':'synthetic_slow_policy_1234','method':'client_updates','params':{}}
                started=time.monotonic();value=client.request(req,timeout=.08,deadline=.15);elapsed=time.monotonic()-started
                self.assertLess(elapsed,.6);self.assertEqual(value['error']['code'],'transport_unconfirmed');self.assertFalse(value['persistence']['durable'])
                with self.assertRaises(ValueError):client.request(dict(req,method='remember'),timeout=.08,deadline=.15)
            finally:http.shutdown();http.server_close();thread.join(timeout=2)

    def test_stable_write_id_and_revision_forwarded_unchanged(self):
        self.ready();params = dict(request_id='synthetic_write_12345', id='synthetic-topic', title='Synthetic',
                                 description='Synthetic only', body='Synthetic value.', expected_revision='missing')
        call = self.message('tools/call', {'name': 'remember', 'arguments': params})
        first = self.server.handle(call);repeat = self.server.handle(call)
        self.assertEqual(first, repeat);self.assertTrue(first['structuredContent']['persistence']['durable'])
        self.assertEqual(self.client.requests[0], self.client.requests[1])
        self.assertEqual(self.client.requests[0]['request_id'], params['request_id'])
        self.assertEqual(self.client.requests[0]['params']['expected_revision'], 'missing')
        self.assertNotIn('request_id', self.client.requests[0]['params'])

    def test_missing_empty_or_invalid_write_id_and_scope_injection_blocked(self):
        self.ready();params = dict(id='synthetic', expected_revision='missing', request_id='synthetic_forget_12345')
        for modified in [{k: v for k, v in params.items() if k != 'request_id'}, dict(params, request_id=''),
                         dict(params, request_id=None), dict(params, actor='administrator'), dict(params, root='/private'),
                         dict(params, token='synthetic-secret-in-arguments')]:
            with self.assertRaises(bridge.ProtocolError):
                self.server.handle(self.message('tools/call', {'name': 'forget', 'arguments': modified}))
        self.assertEqual(self.client.requests, [])

    def test_pending_and_missing_credential_errors_are_visible_and_non_durable(self):
        self.ready();self.client.error = True
        value = self.server.handle(self.message('tools/call', {'name': 'topic', 'arguments': {'id': 'synthetic'}}))
        self.assertTrue(value['isError']);self.assertFalse(value['structuredContent']['persistence']['durable'])
        self.client.request = lambda _: (_ for _ in ()).throw(ValueError('PRIVATE_CREDENTIAL_FIXTURE'))
        value = self.server.handle(self.message('tools/call', {'name': 'topic', 'arguments': {'id': 'synthetic'}}))
        self.assertTrue(value['isError']);self.assertNotIn('PRIVATE_CREDENTIAL_FIXTURE', json.dumps(value))
        self.assertIn('next_action', value['structuredContent'])

    def test_read_budget_and_mcp_metadata_never_become_http_identity(self):
        self.ready()
        self.server.handle(self.message('tools/call', {'name': 'recall', 'arguments': {'query': 'synthetic'},
                           '_meta': {'progressToken': 'metadata-only'}}))
        self.assertEqual(self.client.requests[-1]['params'], {'query': 'synthetic', 'limit': 3})
        with self.assertRaises(bridge.ProtocolError):
            self.server.handle(self.message('tools/call', {'name': 'recall', 'arguments': {'query': 'synthetic', 'limit': True}}))
        self.server.handle(self.message('tools/call', {'name': 'client_updates', 'arguments': {}}))
        self.assertEqual(self.client.requests[-1]['params']['adapter'], 'codex')

    def test_real_stdio_http_bridge_and_auth_headers_with_synthetic_token(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append((data, dict(self.headers)))
                payload = {'request_id': data['request_id'], 'result': {'id': 'synthetic-http', 'revision': 'synthetic-http-revision'}}
                raw = json.dumps(payload).encode();self.send_response(200);self.send_header('Content-Length', str(len(raw)))
                self.end_headers();self.wfile.write(raw)
            def log_message(self, *args):pass
        with tempfile.TemporaryDirectory() as temporary:
            token = Path(temporary) / 'synthetic.token';token.write_text('SYNTHETIC_ONLY_TOKEN_FIXTURE_1234567890\n');token.chmod(0o600)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
            try:
                initialize = self.message('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                                           'clientInfo': {'name': 'synthetic-protocol-client', 'version': '1'}})
                messages = [initialize, {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                            self.message('tools/list', identifier=2),
                            self.message('tools/call', {'name': 'topic', 'arguments': {'id': 'synthetic-http'}}, identifier=3)]
                result = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'adapters/codex/remote_mcp.py'),
                          '--endpoint', 'http://127.0.0.1:' + str(server.server_port), '--token-file', str(token)],
                          input=''.join(json.dumps(v)+'\n' for v in messages), text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                responses = [json.loads(v) for v in result.stdout.splitlines()]
                self.assertEqual([v['id'] for v in responses], [1, 2, 3])
                self.assertEqual(responses[-1]['result']['structuredContent']['result']['revision'], 'synthetic-http-revision')
                self.assertNotIn(token.read_text().strip(), result.stdout + result.stderr)
                self.assertEqual(seen[0][1]['X-Agent-Brain-Adapter'], 'codex')
                self.assertEqual(seen[0][1]['X-Agent-Brain-Protocol'], '1')
                self.assertTrue(seen[0][1]['Authorization'].startswith('Bearer SYNTHETIC_ONLY_'))
            finally:
                server.shutdown();server.server_close();thread.join(timeout=2)

    def test_stdio_duplicate_fields_and_oversize_fail_without_secret_echo(self):
        command = [sys.executable, '-I', '-B', str(ROOT / 'adapters/codex/remote_mcp.py'), '--endpoint', 'http://127.0.0.1:8765',
                   '--token-file', '/synthetic/nonexistent.token']
        for raw in ['{"jsonrpc":"2.0","jsonrpc":"PRIVATE_INPUT_FIXTURE"}\n', 'PRIVATE_INPUT_FIXTURE' * 9000+'\n']:
            result = subprocess.run(command, input=raw, text=True, capture_output=True, timeout=5)
            self.assertEqual(len(result.stdout.splitlines()), 1)
            self.assertIn('error', json.loads(result.stdout));self.assertNotIn('PRIVATE_INPUT_FIXTURE', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
