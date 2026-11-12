"""Isolated source/release MCP checkpoint regression; synthetic catalog only."""

import json
import os
from pathlib import Path
import sys

package = Path(sys.argv[1])
sys.path[:0] = [str(package / 'src')]
from agent_brain_client.adapters.harnesses.codex.content_ports import CodexContentPorts
from agent_brain_client.adapters.platforms.filesystem import require_platform
from agent_brain_client.interfaces.mcp.topics import Server
from agent_brain_client.interfaces.mcp.content import installation_check
from agent_brain_client.interfaces.mcp.content import ContentServer
from agent_brain_client.adapters.storage.content_session import ContentSession
import tempfile

target = require_platform('checkpoint-regression')
assert Server(None, '0.5.6').client_platform == target
grant = {'adapter': 'codex', 'platform': target, 'actor': 'synthetic-owner',
         'scopes': ['read', 'put'], 'artifact_types': ['memory'], 'ids': {'memory': '*'}}
policy = {'schema': 1, 'adapter': 'codex', 'kinds': ['memory'], 'ids': {'memory': '*'},
          'dependencies': {'commands': [], 'python_modules': []}}


# Installation probes use the actual pinned synthetic profile/policy. A
# Memory-only stand-in cannot prove Rule/Skill-specific tool declarations.
if '--content-policy' in sys.argv:
    from agent_brain_client.interfaces.mcp.content import pinned
    from agent_brain_client.domain.content_policy import validate_policy
    def argument(name):
        return sys.argv[sys.argv.index(name) + 1]
    policy = pinned(argument('--content-policy'), argument('--content-policy-sha256'))
    profile = pinned(argument('--https-profile'), argument('--https-profile-sha256'))
    grant = profile['grant']
    validate_policy(policy, grant)


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
    adapter = ContentSession(home, home / 'cache', policy, cloud, host=CodexContentPorts, native_home=home / 'codex')
    server = ContentServer(adapter, '0.5.6')
    server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-11-25', 'capabilities': {},
        'clientInfo': {'name': 'synthetic-install-integration', 'version': '1'}}})
    server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    actual = server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
    assert sorted(tool['name'] for tool in actual['tools']) == result['tools']
    assert server.client_runtime['offered_tools'] == result['tools']
    assert 'memory_query' in result['tools']
    assert server.client_runtime['host_tool_visibility'] == 'unknown'
    assert server.client_runtime['model_loaded'] == 'unknown'
if '--installation-result' in sys.argv:
    print(json.dumps(result))
    raise SystemExit(0)
print(json.dumps({'checkpoint': target, 'installation_check': result['status'],
                  'native_changed': False, 'credentials_accessed': False, 'network_used': False}))
