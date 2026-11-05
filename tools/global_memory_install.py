"""Owned inline user Hook transaction; leaves legacy hooks.json and MCP intact."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
from brain_lock import command_line,private_directory,private_mode
import tomllib
import uuid

import memory_automation as old
from global_memory_scope import validate

# Status also runs from an isolated CLI without the test runner's PYTHONPATH.
# Load the same fixed bundle paths used by install and Hook execution.
old.imports()

BEGIN = '# agent-brain-global-memory:begin'
END = '# agent-brain-global-memory:end'


class ManagedHookConflict(ValueError):
    def __init__(self, field):
        self.field = field
        super().__init__('Outside edit to owned global Hook preserved: ' + field)


def read_receipt(path):
    path=old.canonical(path,existing=True)
    private_mode(path,owner=True)
    if not path.is_file() or path.stat().st_nlink!=1 or path.stat().st_size>4*1048576:
        raise ValueError('Bounded private Hook receipt required')
    return json.loads(path.read_bytes(),object_pairs_hook=old.unique)


def legacy_split_owned(raw):
    text = raw.decode()
    if BEGIN not in text and END not in text: return text, None
    if text.count(BEGIN) != 1 or text.count(END) != 1: raise ValueError('Ambiguous owned global Hook block')
    start, end = text.index(BEGIN), text.index(END) + len(END)
    if end < start: raise ValueError('Invalid owned global Hook block')
    if text[end:end + 1] == '\n': end += 1
    # Host writers may insert trust or third-party tables before our end marker.
    # Markers delimit text, not ownership of those tables. Keep their exact
    # bytes outside the owned Hook definition; the transaction below also checks
    # the complete parsed unowned configuration before writing.
    body = text[start:text.index(END)]
    kept, foreign = [], []
    section, unowned = 0, False
    offset = 0
    for line in body.splitlines(keepends=True):
        if re.match(r'^\s*\[', line):
            try:
                header = tomllib.loads(line)
                # A header-looking line inside a multiline string/array is data.
                tomllib.loads(body[:offset])
            except ValueError:
                pass
            else:
                next_unowned = ('hooks' not in header
                                or set(header['hooks']) == {'state'})
                boundary = offset
                # Legacy schema-2 journals kept comments before hooks.state
                # in previous_block. Preserve that exact split for recovery.
                if next_unowned and not unowned and 'hooks' not in header:
                    for preceding in reversed(body[section:offset].splitlines(keepends=True)):
                        if preceding.strip() and (not preceding.lstrip().startswith('#') or BEGIN in preceding):
                            break
                        boundary -= len(preceding)
                (foreign if unowned else kept).append(body[section:boundary])
                section, unowned = boundary, next_unowned
        offset += len(line)
    (foreign if unowned else kept).append(body[section:])
    return text[:start] + ''.join(foreign) + text[end:], ''.join(kept) + text[text.index(END):end]


def owned_hook(hook):
    from global_memory import MARKER
    return isinstance(hook, dict) and hook.get('statusMessage') == MARKER


def owned_group(group):
    """Project our commands and their invocation settings from a shared group."""
    hooks = group.get('hooks') if isinstance(group, dict) else None
    if not isinstance(hooks, list): return None
    tagged = [hook for hook in hooks if owned_hook(hook)]
    return dict(group, hooks=tagged) if tagged else None


def owned_groups(config):
    """Own tagged commands, never a whole event or foreign sibling commands."""
    result = {}
    for event, groups in config.get('hooks', {}).items():
        if not isinstance(groups, list): continue
        for index, group in enumerate(groups):
            projection = owned_group(group)
            if projection is not None: result[('hooks', event, index)] = projection
    return result


def render_groups(groups):
    from codex_connect import toml_literal, toml_key
    result = ''
    for path, group in groups.items():
        header = '.'.join(toml_key(key) for key in path[:2])
        result += '[[' + header + ']]\n'
        for key, value in group.items():
            if key != 'hooks': result += toml_key(key) + ' = ' + toml_literal(value) + '\n'
        for hook in group.get('hooks', []):
            result += '[[' + header + '.hooks]]\n'
            result += ''.join(toml_key(key) + ' = ' + toml_literal(value) + '\n' for key, value in hook.items())
    return result


def split_owned(raw):
    from codex_connect import toml_statements, toml_remove, toml_get
    text = raw.decode()
    markers = {BEGIN: [], END: []}
    for start, end, kind, _ in toml_statements(text):
        if kind == 'comment' and text[start:end].strip() in markers:
            markers[text[start:end].strip()].append((start, end))
    if not any(markers.values()): return text, None
    if any(len(spans) != 1 for spans in markers.values()) or markers[BEGIN][0][0] >= markers[END][0][0]:
        raise ValueError('Ambiguous owned global Hook block')
    config = tomllib.loads(text)
    groups = owned_groups(config)
    # Remove marker comments in reverse source order, then edit semantic paths.
    for start, end in sorted(markers[BEGIN] + markers[END], reverse=True):
        text = text[:start] + text[end:]
    paths = []
    for path, projection in groups.items():
        commands = toml_get(config, path)['hooks']
        if len(commands) == len(projection['hooks']): paths.append(path)
        else:
            paths.extend(path + ('hooks', index) for index, command in enumerate(commands) if owned_hook(command))
    outside = toml_remove(text, paths)
    return outside, BEGIN + '\n' + render_groups(groups) + END + '\n'


def render_owned(outside, owned):
    """Merge into existing inline event arrays, append ordinary array tables."""
    if owned is None: return (outside.rstrip() + '\n').encode()
    from codex_connect import toml_statements, toml_replace
    groups = owned_groups(tomllib.loads(owned))
    appended = {}
    for path, group in groups.items():
        event_path = path[:2]
        if any(kind == 'value' and event_path[:len(current)] == current
               for _, _, kind, current in toml_statements(outside)):
            config = tomllib.loads(outside)
            existing = config.get('hooks', {}).get(path[1], [])
            outside = toml_replace(outside, event_path, existing + [group])
        else: appended[path] = group
    return (outside.rstrip() + '\n\n' + BEGIN + '\n' + render_groups(appended) + END + '\n').encode()


def owned_conflict(previous, candidates):
    """Name the first conflicting field, without printing command/secret values."""
    def difference(left, right, path):
        if type(left) is not type(right): return path
        if isinstance(left, dict):
            for key in sorted(left.keys() | right.keys()):
                if key not in left or key not in right: return path + '.' + key
                found = difference(left[key], right[key], path + '.' + key)
                if found: return found
        elif isinstance(left, list):
            if len(left) != len(right): return path
            for index, (a, b) in enumerate(zip(left, right)):
                found = difference(a, b, path + '[' + str(index) + ']')
                if found: return found
        elif left != right: return path
        return None
    try:
        path = difference(tomllib.loads(previous or ''), tomllib.loads(candidates[0] or ''), 'config')
    except (ValueError, TypeError, IndexError): path = 'hooks'
    return ManagedHookConflict(path or 'hooks')


def typed_equal(x, y):
    from codex_connect import toml_equal
    return toml_equal(x, y)


def same_owned(left, right):
    """Compare only the parsed owned Hook definitions; never compare trust."""
    if left == right:
        return True
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        a, b = tomllib.loads(left), tomllib.loads(right)
        return typed_equal(a, b) and set(a) == {'hooks'} and 'state' not in a['hooks']
    except (ValueError, TypeError, KeyError):
        return False


def unowned_config(config, owned):
    """Remove our commands while retaining foreign commands in shared groups."""
    result = copy.deepcopy(config)
    if owned is None:
        if result.get('hooks') == {}:
            result.pop('hooks')
        return result
    expected = tomllib.loads(owned).get('hooks', {})
    hooks = result.get('hooks', {})
    for event, groups in expected.items():
        actual = hooks.get(event)
        if not isinstance(groups, list) or not isinstance(actual, list):
            raise ValueError('Owned Hook event changed')
        for group in groups:
            matches = [index for index, candidate in enumerate(actual) if typed_equal(owned_group(candidate), group)]
            if len(matches) != 1: raise ValueError('Owned Hook event ambiguous: hooks.' + event)
            index = matches[0]
            remaining = [command for command in actual[index]['hooks'] if not owned_hook(command)]
            if remaining: actual[index]['hooks'] = remaining
            else: del actual[index]
        if not actual: del hooks[event]
    if not hooks: result.pop('hooks', None)
    return result


def block(value, *, root=None):
    from global_memory import ROOT, MARKER
    ROOT = Path(root) if root is not None else ROOT
    pins = value['code_sha256']
    guard = ('import hashlib,pathlib,runpy,sys; r=pathlib.Path(' + repr(str(ROOT)) + '); p=' + repr(pins)
             + '; assert all(hashlib.sha256((r/n).read_bytes()).hexdigest()==h for n,h in p.items()); '
             + 'sys.path.insert(0,str(r/"tools")); sys.argv=[str(r/"tools/global_memory.py"),"global-hook","--device-home",'
             + repr(value['device_home']) + ']; runpy.run_path(sys.argv[0],run_name="__main__")')
    command = command_line([os.path.realpath(os.sys.executable), '-I', '-B', '-c', guard])
    lines = [BEGIN]
    for event in ('SessionStart', 'UserPromptSubmit', 'Stop'):
        lines += ['[[hooks.' + event + ']]', '[[hooks.' + event + '.hooks]]', 'type = "command"',
                  'command = ' + json.dumps(command), 'timeout = ' + '120',
                  'statusMessage = ' + json.dumps(MARKER)]
        if os.name=='nt': lines += ['commandWindows = ' + json.dumps(command)]
        # Short-lived exec hosts can shut down before an async Stop starts.
        # Wait for the existing durable outbox path; the hook still fails open.
        if event == 'Stop': lines += ['async = false']
    return '\n'.join(lines) + '\n' + END + '\n'


def recover_record(file, receipt, record):
    """Restore only this journal's exact host images, without parsing live TOML."""
    from codex_connect import host_restore
    if record.get('schema') not in (2,3) or set(record.get('host',{}))!={str(file)}:
        raise ValueError('Hook recovery scope changed')
    row=record['host'][str(file)]
    if row['before_sha']!=record['before_sha256'] or row['target_sha']!=record['after_sha256']:
        raise ValueError('Hook recovery images changed')
    before=base64.b64decode(row['before'],validate=True)
    target=base64.b64decode(row['target'],validate=True)
    splitter = legacy_split_owned if record['schema'] == 2 else split_owned
    outside, previous=splitter(before)
    expected=((outside.rstrip()+('\n\n'+record['block'] if record['block'] is not None else '\n')).encode()
              if record['schema'] == 2 else render_owned(outside, record['block']))
    if previous!=record['previous_block'] or target!=expected:
        raise ValueError('Hook recovery target changed')
    if not typed_equal(tomllib.loads(before.decode()).get('mcp_servers'),tomllib.loads(target.decode()).get('mcp_servers')):
        raise ValueError('Hook recovery MCP scope changed')
    host_restore(record['host'],lambda:old.write(receipt,record))
    restored=dict(record,phase='rolled-back',block=record['previous_block'])
    old.write(receipt,restored)
    return restored


def recover_pending(home, native):
    """Authenticate the saved registration image before early Hook restoration.

    The current configuration may be a recognized torn write and invalid TOML.
    All policy/command validation uses the pinned preimage, never an exception
    to registration verification. No credentials or source bodies are read.
    """
    import content_install as ci
    from brain_lock import locked
    from global_memory import settings_path
    home=ci.safe(home);native=ci.safe(native)
    receipt=settings_path(home).parent/'hook-install.json'
    if not receipt.exists():return False
    with locked(receipt.parent/'hook-install.lock'):
        record=read_receipt(receipt)
        if record.get('phase')!='applying':return False
        file=native/'config.toml'
        if set(record.get('host',{}))!={str(file)}:raise ValueError('Hook native binding changed')
        before=base64.b64decode(record['host'][str(file)]['before'],validate=True)
        meta=ci.private_json(home/'.config/agent-brain-client/codex-mcp.json')
        _,_,_,registered_native=ci.registered_context(home,meta,config_image=before)
        if registered_native!=native:raise ValueError('Hook native binding changed')
        recover_record(file,receipt,record)
    return True


def verified_removal(home, previous, record):
    """Recognize the same Hook definition in an authenticated removal journal."""
    if previous is not None: return False
    from client_lifecycle import read_removal, validate_journal
    removal = read_removal(home)
    validate_journal(home, removal)
    return (removal.get('phase') in ('complete', 'reinstalled')
            and same_owned(removal.get('hook_block'), record.get('block')))


def transact(native, directory, owned, *, remove=False):
    from codex_connect import HostConfigChanged
    for attempt in range(3):
        try: return transact_once(native, directory, owned, remove=remove)
        except HostConfigChanged:
            if attempt == 2: raise HostConfigChanged('config.toml is still being edited; retry update after the editor finishes') from None


def transact_once(native, directory, owned, *, remove=False):
    from brain_lock import locked
    file = old.canonical(native / 'config.toml', existing=True)
    receipt = directory / 'hook-install.json'
    from codex_connect import host_plan,host_apply,host_restore,HostConfigChanged
    with locked(directory / 'hook-install.lock'):
        record = read_receipt(receipt) if receipt.exists() else None
        if record and record.get('phase')=='applying':
            record=recover_record(file,receipt,record)
        before = file.read_bytes(); outside, previous = split_owned(before)
        if record:
            candidates = [record.get('block'), record.get('previous_block')] if record.get('phase') == 'prepared' else [record.get('block')]
            if not any(same_owned(previous, candidate) for candidate in candidates):
                # A verified normal uninstall removes the owned inline block
                # while retaining this private receipt and device settings.
                if not verified_removal(Path(directory).parents[2], previous, record):
                    raise owned_conflict(previous, candidates)
        elif previous is not None: raise ValueError('Unowned global Hook block preserved')
        if remove and previous is None: return {'status': 'global-hook-not-installed', 'native_changed': False}
        after = render_owned(outside, None if remove else owned)
        old_config, new_config = tomllib.loads(before.decode()), tomllib.loads(after.decode())
        outside_config=tomllib.loads(outside)
        from global_memory import MARKER
        if any(any(h.get('statusMessage')==MARKER for h in group.get('hooks',[])) for groups in outside_config.get('hooks',{}).values() if isinstance(groups,list) for group in groups if isinstance(group,dict)):
            raise ValueError('Unowned duplicate global Hook preserved')
        if not typed_equal(old_config.get('mcp_servers'), new_config.get('mcp_servers')): raise ValueError('Global install must preserve all MCP registrations')
        if not typed_equal(unowned_config(old_config, previous), unowned_config(new_config, None if remove else owned)):
            raise ValueError('Unowned host configuration changed')
        # Parse first; inline-table conflicts must fail before any write.
        expected = tomllib.loads(render_owned(outside, None if remove else owned).decode())
        if not typed_equal(new_config, expected): raise ValueError('Unexpected native configuration change')
        if before == after or (not remove and same_owned(previous, owned)):
            if record and record.get('phase')=='rolled-back':
                old.write(receipt,{'schema':3,'phase':'complete','block':owned,'previous_block':previous,
                                   'before_sha256':hashlib.sha256(before).hexdigest(),'after_sha256':hashlib.sha256(after).hexdigest(),
                                   'host':host_plan({file:after})})
            return {'status':'global-hook-installed','native_changed':False,'trust':'normal-host-review-required-if-not-already-trusted'}
        journal = {'schema': 3, 'phase': 'applying', 'block': None if remove else owned, 'previous_block': previous,
                   'before_sha256': hashlib.sha256(before).hexdigest(), 'after_sha256': hashlib.sha256(after).hexdigest()}
        journal['host']=host_plan({file:after})
        if journal['host'][str(file)]['before_sha']!=journal['before_sha256']:raise HostConfigChanged('Native config changed before transaction')
        try:
            if file.read_bytes() != before: raise HostConfigChanged('Native config changed before transaction')
            host_apply(journal['host'],lambda:old.write(receipt,journal))
            old.write(receipt,dict(journal,phase='complete'))
        except HostConfigChanged:
            # The single staged commit reports this only before writing. Drop
            # its obsolete intent; never restore over the concurrent editor.
            if record is not None: old.write(receipt, record)
            else: receipt.unlink(missing_ok=True)
            raise
        except Exception:
            host_restore(journal['host'],lambda:old.write(receipt,journal))
            old.write(receipt,dict(journal,phase='rolled-back',block=previous))
            raise
    return {'status': 'global-hook-removed' if remove else 'global-hook-installed', 'native_changed': True,
            'target': str(file), 'mcp_registrations_preserved': True, 'legacy_hooks_json_unchanged': True,
            'trust_changed': False, 'trust': 'normal-host-review-required-if-not-already-trusted'}


def install(args):
    from global_memory import settings_path, FILES, ROOT, approved
    old.imports()
    home = old.canonical(args.device_home, existing=True)
    _, registered = old.registration(home); native = Path(registered['runtime']['codex_home'])
    file = settings_path(home); private_directory(file.parent)
    private_mode(file.parent,directory=True,owner=True)
    if file.exists():
        value = old.private_json(file); validate(value['scope'])
        if value['device_home'] != str(home): raise ValueError('Existing global identity preserved')
    else:
        value = {'schema': 1, 'enabled': False, 'device_home': str(home),
                 'scope': {'schema': 1, 'profile_id': uuid.uuid4().hex, 'include_roots': [], 'exclude_roots': [], 'non_project_roots': [], 'recall_personal': True},
                 'approved_scope_sha256': None, 'server_scope_sha256': None, 'legacy_projects': [],
                 'operator': None, 'operator_sha256': None}
    profile_id = getattr(args, 'profile_id', None)
    if profile_id:
        import re
        if not re.fullmatch('[a-f0-9]{32}', profile_id): raise ValueError('Exact existing personal pool identity required')
        if value['scope']['profile_id'] != profile_id and value['server_scope_sha256'] is None:
            value['scope']['profile_id'] = profile_id
            if value['approved_scope_sha256'] is not None:
                from global_memory_scope import digest
                value['approved_scope_sha256'] = digest(value['scope'])
        # A registered source is reconciled through authenticated configure,
        # never rebound just because a caller supplied a profile hint.
    routes = {row['root']: row for row in value['legacy_projects']}
    base = home / '.config/agent-brain-client/project-memory'
    if os.name!='nt' and base.exists():
        for candidate in base.glob('*/settings.json'):
            try:
                pin = old.digest(candidate.read_bytes()); source = old.load(candidate, pin)
                if source['source_mode'] != 'codex-project': continue
                recall = old.private_json(source['recall_policy'], source['recall_policy_sha256'])
                routes[source['project_root']] = {**routes.get(source['project_root'], {}), 'root': source['project_root'], 'settings': str(candidate), 'settings_sha256': pin, 'project_id': recall['project_id']}
            except (ValueError, OSError): continue
    value['legacy_projects'] = list(routes.values())
    value['code_sha256'] = {n: old.digest((ROOT / n).read_bytes()) for n in FILES}
    if bool(args.operator) != bool(args.operator_sha256): raise ValueError('Complete existing operator reference required')
    if args.operator:
        old.private_json(args.operator, args.operator_sha256)
        value.update(operator=args.operator, operator_sha256=args.operator_sha256)
    old.write(file, value)
    result = transact(native, file.parent, block(value))
    result.update(expanded_sources_enabled=approved(value),
                  automatic_all_projects=True, legacy_project_roots=list(routes), settings=str(file), model_calls=0)
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
    record=read_receipt(receipt)
    _, owned=split_owned((native/'config.toml').read_bytes())
    return record.get('phase')=='complete' and owned is not None and same_owned(owned, record.get('block'))


def recover_code(home):
    """Restore an interrupted code-pin write, preserving every approval field."""
    from codex_connect import host_restore
    from global_memory import settings_path, validate_settings
    file = settings_path(home)
    journal = file.parent / 'code-refresh.json'
    if not journal.exists(): return False
    record = read_receipt(journal)
    if (set(record) != {'schema', 'phase', 'device_home', 'host'}
            or record['schema'] != 1 or record['device_home'] != str(home)
            or record['phase'] not in ('applying', 'complete', 'rolled-back')
            or set(record['host']) != {str(file)}):
        raise ValueError('Global code recovery binding changed')
    row = record['host'][str(file)]
    before, target = (validate_settings(json.loads(base64.b64decode(row[k], validate=True), object_pairs_hook=old.unique), home)
                      for k in ('before', 'target'))
    if {k: v for k, v in before.items() if k != 'code_sha256'} != {k: v for k, v in target.items() if k != 'code_sha256'}:
        raise ValueError('Global code recovery must preserve source approval')
    if record['phase'] != 'applying': return False
    old.registration(home)  # Metadata only; no credential activation.
    host_restore(record['host'], lambda: old.write(journal, record))
    old.write(journal, dict(record, phase='rolled-back'))
    return True


def recover_update(home, *, check_only=False):
    """Recover owned Hook writes before the normal registration TOML inspection."""
    from brain_lock import locked
    from global_memory import settings_path
    directory = settings_path(home).parent
    pending = any(p.exists() and read_receipt(p).get('phase') == 'applying'
                  for p in (directory / 'code-refresh.json', directory / 'hook-install.json'))
    if not pending or check_only: return pending
    with locked(directory / 'code-refresh.lock'):
        recover_code(home)
        _, registered = old.registration(home)
        recover_pending(home, Path(registered['runtime']['codex_home']))
    return True


def freeze_code(directory, pins):
    """Keep executable Hook code in a private immutable cache, never a checkout."""
    from codex_connect import atomic
    from global_memory import ROOT
    root = directory / 'code' / old.digest(json.dumps(pins, sort_keys=True).encode())
    old.canonical(root)  # Reject linked ancestors before creating any cache.
    for parent in (directory / 'code', root):
        private_directory(parent)
        private_mode(parent, directory=True, owner=True)
    for name, pin in pins.items():
        target = root / name
        old.canonical(target)
        private_directory(target.parent)
        private_mode(target.parent, directory=True, owner=True)
        if target.exists():
            old.canonical(target, existing=True)
            private_mode(target, owner=True)
            if target.stat().st_nlink != 1 or old.digest(target.read_bytes()) != pin:
                raise ValueError('Frozen global code changed')
            continue
        raw = (ROOT / name).read_bytes()
        if old.digest(raw) != pin: raise ValueError('Global update source changed')
        atomic(target, raw, 0o600)
    return root


def refresh(home, *, check_only=False):
    """Refresh an already owned Hook; never choose, approve or resume sources."""
    from brain_lock import locked
    from codex_connect import host_plan, host_apply, host_restore
    from global_memory import settings_path, validate_settings, FILES, ROOT
    home = old.canonical(home, existing=True)
    file = settings_path(home)
    absent = {'status': 'global-hook-not-installed', 'native_changed': False, 'update_available': False}
    if not file.exists(): return absent
    if recover_update(home, check_only=check_only) and check_only:
        raise ValueError('Global Hook recovery required')
    private_mode(file.parent, directory=True, owner=True)
    value = validate_settings(old.private_json(file), home)
    original = file.read_bytes()
    if json.loads(original, object_pairs_hook=old.unique) != value: raise ValueError('Global settings changed')
    _, registered = old.registration(home)
    native = Path(registered['runtime']['codex_home'])
    _, previous = split_owned((native / 'config.toml').read_bytes())
    receipt = file.parent / 'hook-install.json'
    if not receipt.exists():
        if previous is not None: raise ValueError('Unowned global Hook preserved')
        return absent
    record = read_receipt(receipt)
    if record.get('phase') not in ('complete', 'rolled-back'):
        raise ValueError('Outside edit to owned global Hook preserved')
    if not same_owned(previous, record.get('block')):
        if not verified_removal(home, previous, record):
            raise owned_conflict(previous, [record.get('block')])
        return absent
    if previous is None: return absent  # Explicit uninstall remains uninstalled.
    pins = {name: old.digest((ROOT / name).read_bytes()) for name in FILES}
    target = dict(value, code_sha256=pins)
    root = file.parent / 'code' / old.digest(json.dumps(pins, sort_keys=True).encode())
    owned = block(target, root=root)
    available = value != target or not same_owned(previous, owned)
    result = {'status': 'global-hook-update-available' if available else 'global-hook-current',
              'update_available': available, 'native_changed': False,
              'sources_changed': False, 'model_calls': 0,
              'trust': 'normal-host-review-required-if-not-already-trusted'}
    if check_only: return result
    with locked(file.parent / 'code-refresh.lock'):
        recover_code(home)
        if file.read_bytes() != original: raise ValueError('Global settings changed before refresh')
        freeze_code(file.parent, pins)
        if not available: return result
        journal = file.parent / 'code-refresh.json'
        record = {'schema': 1, 'phase': 'applying', 'device_home': str(home),
                  'host': host_plan({file: (json.dumps(target, ensure_ascii=False, indent=2) + '\n').encode()})}
        if record['host'][str(file)]['before_sha'] != old.digest(original):
            raise ValueError('Global settings changed before transaction')
        # New command + old settings fail closed during this short transition.
        # Each write has its own journal and preserves outside edits on retry.
        old.write(journal, record)
        try:
            changed = transact(native, file.parent, owned)
            host_apply(record['host'], lambda: old.write(journal, record))
            old.write(journal, dict(record, phase='complete'))
        except Exception:
            host_restore(record['host'], lambda: old.write(journal, record))
            old.write(journal, dict(record, phase='rolled-back'))
            raise
    return dict(result, status='global-hook-updated', update_available=False,
                native_changed=changed['native_changed'])
