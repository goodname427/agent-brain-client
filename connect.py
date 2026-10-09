"""Client-only Codex preflight/registration; no Core, data Git or native writes."""
import argparse
import json
import os
from pathlib import Path
import sys
import uuid

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE if (BASE / 'remote_http.py').is_file() else BASE / 'adapters/codex'))
from remote_http import RemoteClient


def connect(home, endpoint=None, token_file=None, apply=False, service_profile=None, plan_sha256=None):
    if service_profile or (Path(home)/'.config/agent-brain-client/codex-mcp.json').exists() or (sys.platform=='darwin' and not endpoint and not token_file and not (Path(home)/'.config/agent-brain-client/device.json').exists()):
        from codex_connect import connect as ssh_connect
        return ssh_connect(home,service_profile,apply,plan_sha256)
    if sys.platform not in ('darwin', 'linux'):
        return {'status': 'platform-not-supported', 'native_changed': False, 'next_action': 'Prepare and verify this Codex platform before installation.'}
    home = Path(home).resolve()
    registry = home / '.config/agent-brain-client/device.json'
    if (home / '.config/agent-brain/device.json').exists():
        return {'status': 'migration-review-required', 'native_changed': False,
                'next_action': 'Inspect the existing Brain binding and prepare its reviewed migration; preserve current clients.'}
    if registry.is_symlink() or any(p.is_symlink() for p in (registry.parent, registry.parent.parent)):
        return {'status': 'local-state-review-required', 'native_changed': False, 'next_action': 'Preserve the linked client registration and inspect its owner.'}
    existing = json.loads(registry.read_text()) if registry.exists() else None
    keys = {'schema_version', 'adapter', 'endpoint', 'token_file', 'protocol_version', 'state_schema_version'}
    if existing is not None:
        if not isinstance(existing, dict) or set(existing) != keys or existing['schema_version'] != 1 or existing['adapter'] != 'codex':
            return {'status': 'local-state-review-required', 'native_changed': False, 'next_action': 'Inspect unsupported client metadata without overwriting it.'}
        if endpoint and endpoint.rstrip('/') != existing['endpoint'] or token_file and str(Path(token_file).resolve()) != existing['token_file']:
            return {'status': 'service-change-review-required', 'native_changed': False, 'next_action': 'Review the changed service/authentication destination.'}
        endpoint, token_file = endpoint or existing['endpoint'], token_file or existing['token_file']
    if not endpoint or not token_file:
        return {'status': 'server-authorization-required', 'native_changed': False,
                'next_action': 'Resolve the approved service/SSH tunnel and client token file; do not request secrets in chat.'}
    if existing and (existing['protocol_version'], existing['state_schema_version']) != (1, 1):
        return {'status': 'client-upgrade-required', 'native_changed': False, 'next_action': 'Inspect a trusted compatible client release before changing this registration.'}
    client = RemoteClient(endpoint, token_file)
    probe = client.request({'request_id': uuid.uuid4().hex, 'method': 'topic', 'params': {'id': 'client-bootstrap-probe'}})
    if 'error' in probe:
        return {'status': 'service-unavailable', 'error': probe['error'], 'native_changed': False,
                'next_action': 'Inspect the service, tunnel and approved token; retain current registration.'}
    if not isinstance(probe.get('result'), dict) or probe['result'].get('core_api_version') != 1:
        return {'status': 'protocol-review-required', 'native_changed': False,
                'next_action': 'Inspect the service Core API compatibility before registering this client.'}
    platform = ('darwin' if sys.platform == 'darwin' else 'linux')
    import platform as host_platform
    machine = host_platform.machine().lower()
    updates = client.request({'request_id': uuid.uuid4().hex, 'method': 'client_updates',
                              'params': {'adapter': 'codex', 'platform': platform + '-' + machine, 'installed_version': '0.1.0'}})
    value = {'schema_version': 1, 'adapter': 'codex', 'endpoint': endpoint.rstrip('/'),
             'token_file': str(Path(token_file).resolve()), 'protocol_version': 1, 'state_schema_version': 1}
    written = False
    if apply and existing is None:
        registry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with os.fdopen(os.open(registry, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as output:
            json.dump(value, output)
        written = True
    return {'status': 'client-registered' if existing or written else 'ready-to-register',
            'client_metadata_written': written, 'native_changed': False, 'model_loaded': False,
            'updates': updates.get('result', updates),
            'next_action': 'Use the thin Codex transport; wire the approved release channel and native startup separately. Registration does not prove Harness loading.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', required=True, help='Actual Harness name; only Codex has a wired checkpoint.')
    parser.add_argument('--device-home', type=Path, default=Path.home())
    parser.add_argument('--endpoint')
    parser.add_argument('--token-file', type=Path)
    parser.add_argument('--apply', action='store_true', help='Register within authorization; first SSH native installation also requires --plan-sha256.')
    parser.add_argument('--service-profile',type=Path,help='Approved private SSH profile for the Mac Codex checkpoint.')
    parser.add_argument('--plan-sha256',help='Exact native plan digest; required for first SSH MCP registration.')
    args = parser.parse_args()
    if args.agent!='codex':
        print(json.dumps({'status':'adapter-review-required','native_changed':False,'next_action':'Inspect actual Harness loading/permissions and prepare a grounded adapter; do not impersonate Codex.'}))
        return 2
    try:
        result = connect(args.device_home, args.endpoint, args.token_file, args.apply,args.service_profile,args.plan_sha256)
    except FileNotFoundError:
        result = {'status': 'client-file-missing', 'native_changed': False, 'next_action': 'Locate the approved token file or repair missing client metadata; do not generate replacement credentials silently.'}
    except PermissionError:
        result = {'status': 'client-file-permission-blocked', 'native_changed': False, 'next_action': 'Inspect client/token ownership and permissions without changing global permissions.'}
    except json.JSONDecodeError:
        result = {'status': 'client-metadata-invalid', 'native_changed': False, 'next_action': 'Inspect malformed client metadata and preserve its bytes before repair.'}
    except ValueError:
        result = {'status': 'client-configuration-invalid', 'native_changed': False, 'next_action': 'Verify HTTPS or loopback destination, token format and 0600 token permissions.'}
    except Exception:
        result = {'status': 'preflight-blocked', 'native_changed': False,
                  'next_action': 'Inspect client metadata, token permissions and service access; preserve all existing files.'}
    print(json.dumps(result))
    return 0 if result['status'] in ('client-registered', 'native-client-registered', 'ready-to-register') else 2


if __name__ == '__main__':
    if sys.argv[1:]==['--self-check']:
        # Package health uses imports and the MCP handshake only: no network,
        # token, model, native state or Core execution.
        from codex_connect import TOOLS
        from client_release import CLIENT_FILES
        from remote_mcp import Server
        server=Server(None)
        server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'package-health','version':'1'}}})
        server.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        assert set(t['name'] for t in server.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})['tools'])==set(TOOLS)
        assert not any(n in sys.modules for n in ('brain_core','brain_runtime','brain_cli'))
        print(json.dumps({'client_self_check':'passed','core_imported':False,'native_changed':False}))
    else:sys.exit(main())
