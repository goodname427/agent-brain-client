"""Synthetic stdio only: host decoding must not depend on worker code page."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import content_mcp
import remote_mcp


def synthetic_worker(mode, encoding):
    # Reproduce Windows redirected stdout on every OS, without changing the
    # parent's locale or touching installed configuration, credentials or data.
    sys.stdout.reconfigure(encoding=encoding, errors='strict')

    class Adapter:
        def methods(self):
            return ('memory_query', 'content_read')

        def invoke(self, request):
            return {'result': {'echo': request['params']},
                    'persistence': {'durable': False, 'state': 'read-only'}}

        recall = invoke

        def request(self, request):
            return self.invoke(request)

    if mode == 'content':
        content_mcp.serve(content_mcp.ContentServer(Adapter()),
                          sys.stdin.buffer, sys.stdout)
    else:
        with patch.object(remote_mcp, 'RemoteClient', return_value=Adapter()), \
                patch.object(sys, 'argv', ['remote_mcp', '--endpoint',
                                         'http://localhost:1', '--token-file',
                                         'synthetic-unused.token']):
            remote_mcp.main()


class MCPWireEncoding(unittest.TestCase):
    def test_utf8_host_roundtrip_across_worker_encodings_and_both_bridges(self):
        for mode in ('content', 'remote'):
            for encoding in ('cp936', 'cp1252', 'ascii', 'utf-8'):
                for text in ('合成记忆搜索', '合成记忆 🧠 café\nsecond line'):
                    with self.subTest(mode=mode, encoding=encoding, text=text):
                        self.roundtrip(mode, encoding, text)

    def roundtrip(self, mode, encoding, text):
        query = 'memory_query' if mode == 'content' else 'recall'
        read = 'content_read' if mode == 'content' else 'topic'
        read_args = {'id': 'synthetic-memory'}
        if mode == 'content':
            read_args['kind'] = 'memory'
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-11-25', 'capabilities': {},
                'clientInfo': {'name': 'synthetic-utf8-host', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                'name': query, 'arguments': {'query': text, 'limit': 3}}},
            # Unicode IDs must survive on both success and error envelopes.
            {'jsonrpc': '2.0', 'id': text, 'method': 'tools/call', 'params': {
                'name': read, 'arguments': read_args}},
            {'jsonrpc': '2.0', 'id': text + '-error', 'method': 'synthetic/unknown'},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/list'},
        ]
        raw = ''.join(json.dumps(row, ensure_ascii=False) + '\n'
                      for row in messages).encode('utf-8')
        process = subprocess.run(
            [sys.executable, '-B', str(Path(__file__).resolve()),
             '--synthetic-worker', mode, encoding],
            input=raw, capture_output=True, timeout=5,
        )
        self.assertEqual(process.returncode, 0, process.stderr.decode('utf-8', 'replace'))
        # Deliberately strict: json.loads(bytes) alone also accepts UTF-16/32.
        replies = [json.loads(line) for line in process.stdout.decode('utf-8').splitlines()]
        self.assertEqual([row['id'] for row in replies], [1, 2, text, text + '-error', 5])
        result = replies[1]['result']
        self.assertFalse(result['isError'])
        self.assertEqual(result['structuredContent']['result']['echo']['query'], text)
        self.assertEqual(json.loads(result['content'][0]['text']), result['structuredContent'])
        self.assertFalse(replies[2]['result']['isError'])
        self.assertEqual(replies[3]['error']['code'], -32601)
        self.assertIn('tools', replies[4]['result'])
        self.assertEqual(process.stderr, b'')


if __name__ == '__main__':
    if sys.argv[1:2] == ['--synthetic-worker']:
        synthetic_worker(*sys.argv[2:])
    else:
        unittest.main()
