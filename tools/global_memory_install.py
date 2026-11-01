"""Owned inline user Hook transaction; leaves legacy hooks.json and MCP intact."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import tempfile
import tomllib
import uuid

import memory_automation as old
from global_memory_scope import digest, validate

BEGIN = '# agent-brain-global-memory:begin'
END = '# agent-brain-global-memory:end'


def atomic(file, raw, mode):
    fd, pending = tempfile.mkstemp(prefix='.global-memory-', dir=file.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), mode); f.write(raw); f.flush(); os.fsync(f.fileno())
        os.replace(pending, file)
        directory = os.open(file.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(pending): os.unlink(pending)


def split_owned(raw):
    text = raw.decode()
    if BEGIN not in text and END not in text: return text, None
    if text.count(BEGIN) != 1 or text.count(END) != 1: raise ValueError('Ambiguous owned global Hook block')
    start, end = text.index(BEGIN), text.index(END) + len(END)
    if end < start: raise ValueError('Invalid owned global Hook block')
    if text[end:end + 1] == '\n': end += 1
    return text[:start] + text[end:], text[start:end]


def block(value):
    from global_memory import ROOT, MARKER
    pins = value['code_sha256']
    guard = ('import hashlib,pathlib,runpy,sys; r=pathlib.Path(' + repr(str(ROOT)) + '); p=' + repr(pins)
             + '; assert all(hashlib.sha256((r/n).read_bytes()).hexdigest()==h for n,h in p.items()); '
             + 'sys.path.insert(0,str(r/"tools")); sys.argv=[str(r/"tools/global_memory.py"),"global-hook","--device-home",'
             + repr(value['device_home']) + ']; runpy.run_path(sys.argv[0],run_name="__main__")')
    command = shlex.join([os.path.realpath(os.sys.executable), '-I', '-B', '-c', guard])
    lines = [BEGIN]
    for event in ('SessionStart', 'UserPromptSubmit', 'Stop'):
        lines += ['[[hooks.' + event + ']]', '[[hooks.' + event + '.hooks]]', 'type = "command"',
                  'command = ' + json.dumps(command), 'timeout = ' + ('120' if event in ('SessionStart', 'Stop') else '40'),
                  'statusMessage = ' + json.dumps(MARKER)]
        if event == 'Stop': lines += ['async = true']
    return '\n'.join(lines) + '\n' + END + '\n'


def transact(native, directory, owned, *, remove=False):
    from brain_lock import locked
    file = old.canonical(native / 'config.toml', existing=True)
    if file.stat().st_uid != os.getuid() or file.stat().st_mode & 0o022: raise ValueError('Owned native configuration required')
    receipt = directory / 'hook-install.json'
    with locked(directory / 'hook-install.lock'):
        before = file.read_bytes(); outside, previous = split_owned(before)
        record = old.private_json(receipt) if receipt.exists() else None
        if record:
            candidates = [record.get('block'), record.get('previous_block')] if record.get('phase') == 'prepared' else [record.get('block')]
            if previous not in candidates: raise ValueError('Outside edit to owned global Hook preserved')
        elif previous is not None: raise ValueError('Unowned global Hook block preserved')
        if remove and previous is None: return {'status': 'global-hook-not-installed', 'native_changed': False}
        after = (outside.rstrip() + ('\n\n' + owned if not remove else '\n')).encode()
        old_config, new_config = tomllib.loads(before.decode()), tomllib.loads(after.decode())
        outside_config=tomllib.loads(outside)
        from global_memory import MARKER
        if any(any(h.get('statusMessage')==MARKER for h in group.get('hooks',[])) for groups in outside_config.get('hooks',{}).values() if isinstance(groups,list) for group in groups if isinstance(group,dict)):
            raise ValueError('Unowned duplicate global Hook preserved')
        if old_config.get('mcp_servers') != new_config.get('mcp_servers'): raise ValueError('Global install must preserve all MCP registrations')
        # Parse first; inline-table conflicts must fail before any write.
        if remove:
            expected = tomllib.loads(outside)
        else:
            expected = tomllib.loads(outside.rstrip() + '\n\n' + owned)
        if new_config != expected: raise ValueError('Unexpected native configuration change')
        if before == after: return {'status': 'global-hook-installed', 'native_changed': False, 'trust': 'normal-host-review-required-if-not-already-trusted'}
        journal = {'schema': 1, 'phase': 'prepared', 'block': None if remove else owned, 'previous_block': previous,
                   'before_sha256': hashlib.sha256(before).hexdigest(), 'after_sha256': hashlib.sha256(after).hexdigest()}
        old.write(receipt, journal)
        if file.read_bytes() != before: raise ValueError('Native config changed before transaction')
        atomic(file, after, file.stat().st_mode & 0o777)
        old.write(receipt, dict(journal, phase='complete'))
    return {'status': 'global-hook-removed' if remove else 'global-hook-installed', 'native_changed': True,
            'target': str(file), 'mcp_registrations_preserved': True, 'legacy_hooks_json_unchanged': True,
            'trust_changed': False, 'trust': 'normal-host-review-required-if-not-already-trusted'}


def install(args):
    from global_memory import settings_path, FILES, ROOT
    old.imports()
    home = old.canonical(args.device_home, existing=True)
    _, registered = old.registration(home); native = Path(registered['runtime']['codex_home'])
    file = settings_path(home); file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if file.parent.stat().st_uid!=os.getuid() or file.parent.stat().st_mode&0o077:raise ValueError('Private global state directory required')
    if file.exists():
        value = old.private_json(file); validate(value['scope'])
        if value['device_home'] != str(home): raise ValueError('Existing global identity preserved')
    else:
        value = {'schema': 1, 'enabled': True, 'device_home': str(home),
                 'scope': {'schema': 1, 'profile_id': uuid.uuid4().hex, 'include_roots': [], 'exclude_roots': [], 'non_project_roots': [], 'recall_personal': True},
                 'approved_scope_sha256': None, 'server_scope_sha256': None, 'legacy_projects': [],
                 'operator': None, 'operator_sha256': None}
    routes = {row['root']: row for row in value['legacy_projects']}
    base = home / '.config/agent-brain-client/project-memory'
    if base.exists():
        for candidate in base.glob('*/settings.json'):
            try:
                pin = old.digest(candidate.read_bytes()); source = old.load(candidate, pin)
                if source['source_mode'] != 'codex-project': continue
                recall = old.private_json(source['recall_policy'], source['recall_policy_sha256'])
                routes[source['project_root']] = {'root': source['project_root'], 'settings': str(candidate), 'settings_sha256': pin, 'project_id': recall['project_id']}
            except (ValueError, OSError): continue
    value['legacy_projects'] = list(routes.values())
    value['code_sha256'] = {n: old.digest((ROOT / n).read_bytes()) for n in FILES}
    if bool(args.operator) != bool(args.operator_sha256): raise ValueError('Complete existing operator reference required')
    if args.operator:
        old.private_json(args.operator, args.operator_sha256)
        value.update(operator=args.operator, operator_sha256=args.operator_sha256)
    old.write(file, value)
    result = transact(native, file.parent, block(value))
    result.update(expanded_sources_enabled=value['approved_scope_sha256'] == digest(value['scope']) and value['server_scope_sha256'] == digest(value['scope']),
                  pending_sources_default_disabled=True, legacy_project_roots=list(routes), settings=str(file), model_calls=0)
    return result


def uninstall(args):
    from global_memory import settings_path
    old.imports(); _, registered = old.registration(args.device_home)
    directory = settings_path(args.device_home).parent
    return transact(Path(registered['runtime']['codex_home']), directory, '', remove=True)


def installation_status(value):
    from global_memory import settings_path
    _, registered=old.registration(value['device_home'])
    native=Path(registered['runtime']['codex_home'])
    receipt=settings_path(value['device_home']).parent/'hook-install.json'
    if not receipt.exists():return False
    record=old.private_json(receipt)
    _, owned=split_owned((native/'config.toml').read_bytes())
    return record.get('phase')=='complete' and owned is not None and owned==record.get('block')
