"""Isolated source/release MCP checkpoint regression; synthetic catalog only."""
import json
from pathlib import Path
import sys

package = Path(sys.argv[1])
sys.path[:0] = [str(package / name) for name in ('scripts', 'adapters/codex')]
from brain_lock import require_platform
from remote_mcp import Server
from content_mcp import installation_check

target = require_platform('checkpoint-regression')
assert Server(None, '0.5.4').client_platform == target
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
result = installation_check(cloud, policy, '0.5.4')
assert result['status'] == 'content-native-protocol-checked' and len(cloud.requests) == 1
assert result['native_changed'] is False
print(json.dumps({'checkpoint': target, 'installation_check': result['status'],
                  'native_changed': False, 'credentials_accessed': False, 'network_used': False}))
