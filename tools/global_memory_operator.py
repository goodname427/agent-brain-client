"""One preview/approval entry using an existing pinned SSH operator reference.

No Server imports, key material, arbitrary remote commands or billing changes.
"""
import base64
import copy
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

import memory_automation as old
from global_memory_scope import validate, digest, within, select


def operator(value, packet):
    if not value['operator'] or not value['operator_sha256']:
        raise ValueError('Existing operator reference required')
    ops = old.private_json(value['operator'], value['operator_sha256'])
    if set(ops) != {'schema', 'ssh_argv', 'known_hosts', 'known_hosts_sha256', 'server_tool_sha256'} or ops['schema'] != 1:
        raise ValueError('Exact operator reference required')
    argv = ops['ssh_argv']
    if (not isinstance(argv, list) or not argv or argv[0] not in ('ssh', '/usr/bin/ssh')
            or any(not isinstance(x, str) or '\n' in x or '\x00' in x for x in argv)
            or not any(x == 'StrictHostKeyChecking=yes' for x in argv)
            or not any(x.startswith('UserKnownHostsFile=') for x in argv)
            or any(x in ('-A', '-L', '-R', '-D') for x in argv)):
        raise ValueError('Pinned non-forwarding SSH operator required')
    target = urlsplit(packet['client']['endpoint']).hostname
    if '@' not in argv[-1] or argv[-1].split('@', 1)[1] != target:
        raise ValueError('Existing operator recipient differs')
    if old.digest(old.canonical(ops['known_hosts'], existing=True).read_bytes()) != ops['known_hosts_sha256']:
        raise ValueError('Existing host verification changed')
    payload = base64.b64encode(json.dumps(packet, ensure_ascii=False).encode()).decode()
    script = '''import base64,hashlib,json,subprocess
from pathlib import Path
pid=subprocess.check_output(['systemctl','show','agent-brain.service','--property=MainPID','--value'],text=True).strip()
args=[x.decode() for x in Path('/proc/'+pid+'/cmdline').read_bytes().split(b'\\0') if x]
entry=next(Path(x) for x in args if x.endswith('/scripts/brain_server.py'))
tool=entry.parent.parent/'tools/manage_global_memory.py'
assert hashlib.sha256(tool.read_bytes()).hexdigest()==TOOL_PIN
packet=base64.b64decode(PACKET,validate=True)
r=subprocess.run(['python3','-I','-B',str(tool),'apply'],input=packet,capture_output=True,timeout=200)
try: result=json.loads(r.stdout.splitlines()[-1])
except Exception: result={'status':'global-operator-unconfirmed','preserve_existing_state':True}
print(json.dumps(result))
raise SystemExit(r.returncode)
'''.replace('==TOOL_PIN', '==' + repr(ops['server_tool_sha256'])).replace('PACKET,', repr(payload) + ',')
    command = "python3 -I -B - <<'AGENT_BRAIN_GLOBAL_OPERATOR'\n" + script + 'AGENT_BRAIN_GLOBAL_OPERATOR\n'
    result = subprocess.run([*argv, 'sudo -n /bin/sh -s'], input=command, capture_output=True, text=True, timeout=230)
    try: receipt = json.loads(result.stdout.splitlines()[-1])
    except (ValueError, IndexError): raise ValueError('Global operator outcome unconfirmed; inspect before retry') from None
    if result.returncode or receipt.get('status') != 'global-memory-configured' or receipt.get('scope_sha256') != digest(packet['scope']):
        raise ValueError('Global operator did not confirm the requested source policy')
    return receipt


def activate(value, scope):
    """Authenticate server admission before enabling future local Hook capture."""
    from global_memory import GlobalClient, settings_path
    client = GlobalClient(value)
    if scope['include_roots']:
        root = scope['include_roots'][0]['root']
        local = {'kind': 'project', 'cwd': root, 'project_root': root, 'repository_root': root}
    elif scope['non_project_roots']:
        local = {'kind': 'non-project', 'cwd': scope['non_project_roots'][0], 'project_root': None, 'repository_root': None}
    else: raise ValueError('Approved source root required')
    response = client.wire('memory_global_status', {'origin': local, 'session_id': 'device-activation-status'})
    result = response.get('result', {})
    pin = digest(scope)
    if result.get('scope_sha256') != pin or result.get('global_profile_id') != scope['profile_id'] or result.get('source_device_id') != client.device_id:
        raise ValueError('Authenticated source admission unconfirmed')
    value.update(scope=scope, approved_scope_sha256=pin, server_scope_sha256=pin, enabled=True)
    old.write(settings_path(value['device_home']), value)
    response = client.wire('memory_global_resume', {})
    if 'error' in response: raise ValueError('Source resume unconfirmed; local fence retained')
    off = settings_path(value['device_home']).parent / 'global.off'
    if off.exists(): off.unlink()
    return {'status': 'global-memory-configured', 'scope_sha256': pin, 'source_device_id': client.device_id,
            'activated': True, 'model_calls': 0, 'auto_recharge': False, 'trust': 'normal-host-hook-review-required'}


def configure(args):
    from global_memory import load, settings_path, GlobalClient
    value = load(args.device_home); directory = settings_path(args.device_home).parent
    pending = directory / 'pending-scope.json'
    if getattr(args, 'activate_packet', None):
        packet = old.private_json(args.activate_packet)
        if packet['scope'] != value['scope'] or packet['approved_scope_sha256'] != value['approved_scope_sha256']:
            raise ValueError('Original approved source packet required')
        client = GlobalClient(value)
        if packet['client']['source_device_id'] != client.device_id: raise ValueError('Original device identity required')
        return activate(value, packet['scope'])
    changed = args.include_root or args.isolate_root or args.exclude_root or args.non_project_root
    if changed:
        scope = copy.deepcopy(value['scope'])
        def canonical(root): return str(Path(root).expanduser().resolve())
        scope.update(include_roots=[{'root': canonical(x), 'sharing': 'personal'} for x in args.include_root]
                     + [{'root': canonical(x), 'sharing': 'project'} for x in args.isolate_root],
                     exclude_roots=[canonical(x) for x in args.exclude_root],
                     non_project_roots=[canonical(x) for x in args.non_project_root])
        validate(scope); old.write(pending, scope)
    else:
        scope = old.private_json(pending) if pending.exists() else copy.deepcopy(value['scope'])
        validate(scope)
    pin = digest(scope)
    if args.approve_scope != pin:
        return {'status': 'global-memory-source-approval-required', 'scope': scope, 'scope_sha256': pin,
                'operator_configured': bool(value['operator']), 'activated': False,
                'next_command': 'sh tools/memory.sh global-configure --approve-scope ' + pin,
                'recipient': 'existing Server and configured DeepSeek Flash', 'budget': 'existing unlimited cumulative accounting; no recharge', 'model_calls': 0}
    off = directory / 'global.off'; off.touch(mode=0o600)
    # Hand future legacy collection to the unified pool only after exact source
    # approval. Retain its configuration, data and outbox without replay/import.
    for row in value['legacy_projects']:
        local = {'kind': 'project', 'cwd': row['root'], 'project_root': row['root'], 'repository_root': row['root']}
        choice = select(scope, local)
        excluded = any(within(row['root'], denied) for denied in scope['exclude_roots'])
        if excluded or choice is not None and choice['sharing'] == 'personal':
            source = old.load(row['settings'], row['settings_sha256'])
            Path(source['off_file']).touch(mode=0o600)
            state = old.daily_command(source, 'stop')
            if state.get('source_admitted') is not False: raise ValueError('Legacy source handoff unconfirmed')
            row['capture_delegated'] = False
    old.write(settings_path(args.device_home), value)
    if value['server_scope_sha256']:
        response = GlobalClient(value).wire('memory_global_stop', {})
        if 'error' in response: raise ValueError('Previous global source stop unconfirmed')
    _, registered = old.registration(args.device_home); r = registered['runtime']
    profile = old.private_json(r['https_profile'], r['https_profile_sha256'])
    if value['legacy_projects']:
        existing = old.load(value['legacy_projects'][0]['settings'], value['legacy_projects'][0]['settings_sha256'])
        recall = old.private_json(existing['recall_policy'], existing['recall_policy_sha256'])
        device_id = recall['source_device_id']
    else:
        device_id = GlobalClient(value).device_id
    packet = {'schema': 1, 'scope': scope, 'approved_scope_sha256': pin,
              'client': {'profile_sha256': r['https_profile_sha256'], 'content_policy_sha256': r['content_policy_sha256'],
                         'endpoint': profile['endpoint'], 'directory': str(directory),
                         'legacy_projects': {row['root']: row['project_id'] for row in value['legacy_projects'] if row.get('capture_delegated', True)},
                         'actor': profile['grant']['actor'], 'source_device_id': device_id}}
    if not value['legacy_projects']:
        packet['schema'] = 2
        packet['client']['grant'] = profile['grant']
    if getattr(args, 'packet_output', None):
        target = old.canonical(args.packet_output)
        if target.exists():
            if old.private_json(target) != packet: raise ValueError('Existing device packet preserved')
        else: old.write(target, packet)
        value.update(scope=scope, approved_scope_sha256=pin, enabled=False)
        old.write(settings_path(args.device_home), value)
        return {'status': 'global-memory-device-packet-prepared', 'packet': str(target),
                'packet_sha256': old.digest(target.read_bytes()), 'scope_sha256': pin,
                'source_device_id': device_id, 'activated': False, 'model_calls': 0,
                'next_action': 'Existing Server operator applies this exact metadata packet; then run global-configure --activate-packet with this file.'}
    receipt = operator(value, packet)
    value.update(scope=scope, approved_scope_sha256=pin, server_scope_sha256=pin, enabled=True)
    old.write(settings_path(args.device_home), value)
    response = GlobalClient(value).wire('memory_global_resume', {})
    if 'error' in response: raise ValueError('Global source resume unconfirmed; local fence retained')
    if off.exists(): off.unlink()
    old.write(directory / 'server-configuration-receipt.json', receipt)
    return {'status': 'global-memory-configured', 'scope_sha256': pin, 'scope': scope,
            'model_calls': 0, 'auto_recharge': False, 'native_delivery': 'requires-supported-host-and-normal-hook-trust'}
