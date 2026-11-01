"""Isolated source/release MCP checkpoint regression; synthetic catalog only."""
import json
import os
from pathlib import Path
import sys

package = Path(sys.argv[1])
sys.path[:0] = [str(package / name) for name in ('scripts', 'adapters/codex')]
from brain_lock import require_platform
from remote_mcp import Server
from content_mcp import installation_check
from content_mcp import ContentServer
from content_cache import ContentAdapter
import tempfile

target = require_platform('checkpoint-regression')
assert Server(None, '0.5.6').client_platform == target
grant = {'adapter': 'codex', 'platform': target, 'actor': 'synthetic-owner',
         'scopes': ['read', 'put'], 'artifact_types': ['memory'], 'ids': {'memory': '*'}}
policy = {'schema': 1, 'adapter': 'codex', 'kinds': ['memory'], 'ids': {'memory': '*'},
          'dependencies': {'commands': [], 'python_modules': []}}


class Cloud:
    def __init__(self):
        self.grant = grant
        self.requests = []

    def request(self, request):
        assert request['method'] == 'content_catalog'
        self.requests.append(request)
        return {'request_id': request['request_id'], 'result': {
            'content_api_version': 1, 'commit': 'a' * 40, 'artifacts': []}}


cloud = Cloud()
result = installation_check(cloud, policy, '0.5.6')
assert result['status'] == 'content-native-protocol-checked' and len(cloud.requests) == 1
assert result['native_changed'] is False
with tempfile.TemporaryDirectory(dir=Path(os.environ['HOME'])) as temporary:
    home = Path(temporary)
    adapter = ContentAdapter(home, home / 'cache', policy, cloud, codex_home=home / 'codex')
    server = ContentServer(adapter, '0.5.6')
    server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-11-25', 'capabilities': {},
        'clientInfo': {'name': 'synthetic-install-integration', 'version': '1'}}})
    server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    actual = server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
    assert sorted(tool['name'] for tool in actual['tools']) == result['tools']
if '--installation-result' in sys.argv:
    print(json.dumps(result))
    raise SystemExit(0)
print(json.dumps({'checkpoint': target, 'installation_check': result['status'],
                  'native_changed': False, 'credentials_accessed': False, 'network_used': False}))
