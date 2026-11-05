"""One Mac Codex SSH checkpoint: inspect/reuse, or apply an exact reviewed plan.

No SSH key creation, token copying, hooks, model tests, Core or data Git.
Legacy-only bindings always require their own migration plan.
"""
import base64
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib

import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"scripts"))
from brain_lock import require_platform,native_platform,MAC_PLATFORMS

NAME='agent_brain_server'
TOOLS=['topic','recall','remember','forget','client_updates']
BEGIN='<!-- agent-brain:begin -->';END='<!-- agent-brain:end -->'
BLOCK=re.compile(re.escape(BEGIN)+r'.*?'+re.escape(END)+r'(?:\r?\n)?',re.S)
MANAGED=BEGIN+'\n# Synthetic test MCP\nUse agent_brain_server only for explicitly requested synthetic tests. Do not automatically query, collect or upload real preferences, facts, rules, memories, project data, native history or credentials. Native memory continues under its existing authorization. Read at most three relevant topics; writes need the read revision and a stable request_id. Only durable=true confirms persistence. Report transport, compatibility and update blockers; do not replace existing permissions or migrate legacy bindings.\n'+END+'\n'
PERSONAL_MANAGED=BEGIN+'\n# Explicit personal memory MCP\nUse agent_brain_server only for explicitly authorized personal shared memory operations. Do not automatically collect or upload conversations, native history, local-only/private material, credentials or project data. Native memory remains available. Read revision before writing and retain request_id on retries; only durable=true confirms persistence. A pinned import manifest is required for migration. Do not infer future collection authorization from scans or previously imported topics.\n'+END+'\n'
sha=lambda data:hashlib.sha256(data).hexdigest()


class HostConfigChanged(ValueError):
    """A staged host write was cancelled before modifying the destination."""


def toml_string(value):
    # JSON escapes C0 controls; TOML additionally forbids a literal DEL. Keep
    # Unicode scalars literal so non-BMP keys never become surrogate escapes.
    return json.dumps(value, ensure_ascii=False).replace('\x7f', r'\u007f')


def toml_literal(value):
    """Encode a parsed TOML value, including values in a shared inline table."""
    import datetime
    import math
    if isinstance(value, str): return toml_string(value)
    if isinstance(value, dict):
        return '{' + ', '.join(toml_string(k) + ' = ' + toml_literal(v) for k, v in sorted(value.items())) + '}'
    if isinstance(value, list): return '[' + ', '.join(map(toml_literal, value)) + ']'
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)): return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return 'nan' if math.isnan(value) else ('inf' if value > 0 else '-inf')
    return json.dumps(value, ensure_ascii=False)


def toml_equal(left, right):
    """TOML value equality keeps types distinct and treats NaN as itself."""
    import math
    if type(left) is not type(right): return False
    if isinstance(left, float) and math.isnan(left) and math.isnan(right): return True
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(toml_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(toml_equal(a, b) for a, b in zip(left, right))
    return left == right


def toml_key(key):
    return key if re.fullmatch(r'[A-Za-z0-9_-]+', key) else toml_string(key)


def toml_statements(text):
    """Yield source spans and semantic paths, without interpreting string data.

    tomllib is the syntax authority. A statement ends only when it parses, so
    header/marker-looking lines in multiline strings and arrays remain data.
    Paths include array-table indices; quoted and dotted keys are equivalent.
    """
    tomllib.loads(text)
    context, prefix, pending, start, offset = (), '', '', 0, 0
    # TOML line breaks are LF/CRLF. Unicode separators can be ordinary comment
    # or string data and must not expose a spurious header or marker line.
    for line in re.findall(r'[^\n]*\n|[^\n]+$', text):
        pending += line
        offset += len(line)
        try: parsed = tomllib.loads(pending)
        except tomllib.TOMLDecodeError: continue
        stripped = pending.lstrip()
        kind, path = 'comment', None
        if parsed:
            if stripped.startswith('['):
                kind = 'header'
                tree = parsed
                names = []
                while isinstance(tree, (dict, list)) and tree:
                    if isinstance(tree, list): tree = tree[-1]
                    else:
                        key, tree = next(iter(tree.items()))
                        names.append(key)
                # Resolve each array table against this exact source prefix.
                tree = tomllib.loads(prefix + pending)
                resolved = []
                for name in names:
                    tree = tree[name]; resolved.append(name)
                    if isinstance(tree, list):
                        resolved.append(len(tree) - 1); tree = tree[-1]
                context = path = tuple(resolved)
            else:
                kind = 'value'
                # Locate '=' outside a quoted key; parsing the LHS with a
                # sentinel recovers its path even if its value is a dictionary.
                quote, escaped = None, False
                for index, char in enumerate(pending):
                    if escaped: escaped = False; continue
                    if quote:
                        if char == '\\' and quote == '"': escaped = True
                        elif char == quote: quote = None
                    elif char in ('"', "'"): quote = char
                    elif char == '=': break
                tree = tomllib.loads(pending[:index] + '=0')
                names = []
                while isinstance(tree, dict):
                    name, tree = next(iter(tree.items())); names.append(name)
                path = context + tuple(names)
        yield start, offset, kind, path
        prefix += pending
        pending, start = '', offset
    if pending: raise ValueError('Incomplete TOML statement')


def toml_get(value, path):
    for key in path: value = value[key]
    return value


def toml_remove(text, paths):
    """Remove semantic subtrees, keeping unrelated source bytes and scope.

    Only a shared inline assignment needs re-encoding. All other unrelated
    statements, comments, whitespace and line endings stay byte-for-byte.
    Array indices refer to the original document, never to an edited prefix.
    """
    config = tomllib.loads(text)
    result = []
    for start, end, kind, path in toml_statements(text):
        source = text[start:end]
        if path is not None:
            if any(path[:len(target)] == target for target in paths): continue
            descendants = [target[len(path):] for target in paths if target[:len(path)] == path]
            if descendants and kind == 'value':
                value = copy.deepcopy(toml_get(config, path))
                empty_assignment = False
                # Remove higher array indices before lower ones.
                for target in sorted(descendants, reverse=True):
                    parent = toml_get(value, target[:-1]); del parent[target[-1]]
                    if isinstance(target[-1], int) and not parent:
                        if len(target) == 1:
                            empty_assignment = True
                            break
                        del toml_get(value, target[:-2])[target[-2]]
                if empty_assignment: continue
                # Reuse the exact quoted/dotted LHS, found by a sentinel parse.
                for index, char in enumerate(source):
                    if char != '=': continue
                    try: tomllib.loads(source[:index] + '=0')
                    except tomllib.TOMLDecodeError: continue
                    source = source[:index + 1] + ' ' + toml_literal(value) + ('\r\n' if source.endswith('\r\n') else '\n')
                    break
        result.append(source)
    return ''.join(result)


def toml_replace(text, path, value):
    """Replace one tree, including when embedded in a shared inline table."""
    config = tomllib.loads(text)
    for start, end, kind, current in toml_statements(text):
        if kind != 'value' or path[:len(current)] != current: continue
        replacement = copy.deepcopy(toml_get(config, current))
        if current == path: replacement = value
        else: toml_get(replacement, path[len(current):-1])[path[-1]] = value
        source = text[start:end]
        for index, char in enumerate(source):
            if char != '=': continue
            try: tomllib.loads(source[:index] + '=0')
            except tomllib.TOMLDecodeError: continue
            newline = '\r\n' if source.endswith('\r\n') else '\n'
            return text[:start] + source[:index + 1] + ' ' + toml_literal(replacement) + newline + text[end:]
    outside = toml_remove(text, [path])
    header = '.'.join(toml_key(key) for key in path)
    groups = value if isinstance(value, list) else [value]
    snippet = ''
    for group in groups:
        snippet += ('[[' + header + ']]' if isinstance(value, list) else '[' + header + ']') + '\n'
        snippet += ''.join(toml_key(key) + ' = ' + toml_literal(item) + '\n' for key, item in sorted(group.items()))
    return outside.rstrip() + '\n\n' + snippet


def migration_config_matches(config, current, expected_sha, state, receipt):
    """Recover a legacy semantic baseline from a hash-authenticated image.

    Old migrations recorded only a full-file hash. Existing transaction journals
    may retain that exact image. Its hash authenticates the managed table even
    after unrelated live configuration changes; no current hash is re-pinned.
    """
    if sha(current) == expected_sha: return True
    from brain_lock import private_mode
    journals = [receipt]
    for name in ('native-install-receipt.json', 'content-install-receipt.json', 'global-memory/hook-install.json'):
        path = Path(state) / name
        if not path.exists(): continue
        try:
            image = host_image(path)
            private_mode(path, owner=True)
            if image is not None:
                journal = json.loads(image[0])
                if isinstance(journal, dict): journals.append(journal)
        except (OSError, ValueError):
            # An unusable unrelated journal is not evidence. Only the exact
            # original image hash can authorize the semantic baseline below.
            continue
    actual = tomllib.loads(current.decode()).get('mcp_servers', {}).get(NAME)
    for journal in journals:
        before = journal.get('before', {}).get(str(config))
        host = journal.get('host', {}).get(str(config), {})
        candidates = [before.get('data') if isinstance(before, dict) else None, host.get('before')]
        for encoded in candidates:
            if not isinstance(encoded, str): continue
            try: original = base64.b64decode(encoded, validate=True)
            except ValueError: continue
            if sha(original) != expected_sha: continue
            expected = tomllib.loads(original.decode()).get('mcp_servers', {}).get(NAME)
            if not isinstance(expected, dict) or not isinstance(actual, dict): return False
            if expected.get('enabled') is True and actual.get('enabled') is False:
                expected = dict(expected, enabled=False)
            return json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
    return False


def service_label(data_mode):
    return 'synthetic test MCP' if data_mode=='synthetic-only' else 'explicit personal memory MCP'


def profile(path):
    p=Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_mode&0o077:raise ValueError('Private approved profile required')
    value=json.loads(p.read_text(encoding='utf-8'))
    if set(value)!={'schema','adapter','platform','data_mode','ssh'} or value['schema']!=1 or value['adapter']!='codex' or value['data_mode'] not in ('synthetic-only','personal-explicit'):raise ValueError('Unsupported service profile')
    if value['platform'] not in MAC_PLATFORMS or value['platform']!=native_platform():raise ValueError('Actual Mac Codex platform must match')
    ssh=value['ssh']
    if set(ssh)!={'host','user','identity_file','known_hosts_file','known_hosts_sha256','remote_command'}:raise ValueError('Unknown SSH scope')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}',ssh['host']) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,31}',ssh['user']):raise ValueError('Invalid SSH destination')
    key,known=Path(ssh['identity_file']),Path(ssh['known_hosts_file'])
    if not key.is_absolute() or key.is_symlink() or not key.is_file() or key.stat().st_mode&0o077:raise ValueError('Existing private SSH identity required')
    if not known.is_absolute() or known.is_symlink() or not known.is_file() or sha(known.read_bytes())!=ssh['known_hosts_sha256']:raise ValueError('Approved strict host identity required')
    command=ssh['remote_command']
    if '..' in command or '\n' in command:raise ValueError('Fixed command path escape rejected')
    prefix='/usr/bin/sudo -n -u agent-brain /usr/bin/python3 -I -B '
    bridge=r'/opt/agent-brain-client/current/remote_mcp\.py --endpoint http://127\.0\.0\.1:[0-9]{1,5} --token-file /var/lib/agent-brain/[A-Za-z0-9._/-]+'
    startup=r'/opt/agent-brain-client/[A-Za-z0-9._/-]+/client_startup\.py --policy-file /opt/agent-brain-client/[A-Za-z0-9._/-]+\.json --policy-sha256 [a-f0-9]{64}'
    if not command.startswith(prefix) or not re.fullmatch('(?:'+bridge+'|'+startup+')',command[len(prefix):]):raise ValueError('Fixed reviewed MCP command required')
    args=['-T','-i',str(key)]
    for option in ['IdentitiesOnly=yes','IdentityAgent=none','BatchMode=yes','StrictHostKeyChecking=yes','UserKnownHostsFile='+str(known),'ForwardAgent=no','ClearAllForwardings=yes','ConnectTimeout=5','ServerAliveInterval=30','ServerAliveCountMax=3']:args.extend(['-o',option])
    args.extend([ssh['user']+'@'+ssh['host'],command])
    return value,{'command':'/usr/bin/ssh','args':args,'startup_timeout_sec':30,'tool_timeout_sec':120,'enabled':True,'enabled_tools':TOOLS,'default_tools_approval_mode':'writes'}


def probe(mcp):
    wire=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'codex-client-preflight','version':'1'}}},{'jsonrpc':'2.0','method':'notifications/initialized'}, {'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}},{'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'client_updates','arguments':{}}}]
    run=subprocess.run([mcp['command'],*mcp['args']],input=''.join(json.dumps(v)+'\n' for v in wire),capture_output=True,text=True,timeout=35)
    rows=[json.loads(line) for line in run.stdout.splitlines()]
    if run.returncode or [v['id'] for v in rows]!=[1,2,3] or rows[0]['result']['serverInfo']['name']!='agent-brain-remote-codex' or set(t['name'] for t in rows[1]['result']['tools'])!=set(TOOLS) or rows[2]['result']['isError']:raise ValueError('MCP protocol preflight blocked')
    return rows[2]['result']['structuredContent']['result']


def atomic(path,raw,mode,*,cache=False):
    require_platform('private-write')
    from brain_lock import private_directory,cache_directory,atomic_replace,file_mode
    (cache_directory if cache else private_directory)(path.parent)
    fd,name=tempfile.mkstemp(prefix='.client-native-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as file:file_mode(file.fileno(),mode);file.write(raw);file.flush();os.fsync(file.fileno())
        if os.name != 'nt': os.chmod(name,mode)
        atomic_replace(name,path)
    finally:
        if Path(name).exists():Path(name).unlink()


def host_image(path):
    """Nonsecret host preflight; never read or rewrite its security descriptor."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Canonical host path required')
    if os.name == 'nt':
        from brain_windows import canonical
        canonical(path)
    try: st = path.stat()
    except FileNotFoundError: return None
    if not path.is_file() or st.st_nlink != 1 or st.st_size > 1048576:
        raise ValueError('Single bounded host file required')
    metadata = getattr(st, 'st_file_attributes', 0) if os.name == 'nt' else (st.st_uid, st.st_gid, st.st_mode & 0o777)
    return (path.read_bytes(), st.st_dev, st.st_ino, st.st_mtime_ns, metadata)


@contextmanager
def windows_host_file(path):
    """Open the existing object for data I/O; allow readers, exclude writers.

    OPEN_EXISTING never replaces the object or assigns a security descriptor.
    No DELETE, WRITE_DAC, WRITE_OWNER or ACCESS_SYSTEM_SECURITY is requested.
    """
    import ctypes as C
    from ctypes import wintypes as W
    import msvcrt
    from brain_windows import canonical, libraries
    path = canonical(path)
    _, kernel = libraries()
    kernel.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    kernel.CreateFileW.restype = W.HANDLE
    kernel.CloseHandle.argtypes = [W.HANDLE]
    handle = kernel.CreateFileW(str(path), 0xC0000000, 1, None, 3, 0x00200000, None)
    if handle == W.HANDLE(-1).value:
        raise ValueError('Host configuration is busy or not writable')
    try: fd = msvcrt.open_osfhandle(handle, os.O_RDWR | getattr(os, 'O_BINARY', 0))
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, 'r+b', buffering=0) as file:
        st = os.fstat(file.fileno())
        if st.st_nlink != 1 or getattr(st, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Single non-reparse host file required')
        yield file


def host_write(file, raw):
    """Write a prefix before truncating, so journaled torn writes are bounded."""
    file.seek(0)
    offset = 0
    while offset < len(raw):
        written = file.write(raw[offset:])
        if not written: raise OSError('Host write incomplete')
        offset += written
    file.flush(); os.fsync(file.fileno())
    file.truncate(len(raw))
    file.flush(); os.fsync(file.fileno())


@contextmanager
def host_stage(path, raw, mode, expected):
    """Preflight host writes without applying the private-store policy.

    Windows existing files keep their object (and unknown SACL/owner/streams).
    The caller must flush its write-ahead journal before commit. Readers may
    observe a partial write; the kernel excludes competing writers and renames.
    New files and POSIX writes use staged, atomic publication instead.
    """
    require_platform('native-install')
    from brain_lock import atomic_replace, sync_directory
    path = Path(path)
    image = host_image(path)
    if (image[0] if image is not None else None) != expected:
        raise HostConfigChanged('Host configuration concurrently changed')
    if os.name == 'nt' and image is not None:
        with windows_host_file(path) as file:
            st = os.fstat(file.fileno())
            original = file.read(1048577)
            if original != expected or (st.st_dev, st.st_ino) != image[1:3]:
                raise HostConfigChanged('Host configuration concurrently changed')
            def commit_inplace():
                file.seek(0)
                if file.read(1048577) != expected or host_image(path) != image:
                    raise HostConfigChanged('Host configuration concurrently changed')
                host_write(file, raw)
            commit_inplace.inplace_identity = {'device': st.st_dev, 'inode': st.st_ino}
            yield commit_inplace
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    host_image(path)
    fd, name = tempfile.mkstemp(prefix='.client-host-', dir=path.parent)
    pending = Path(name)
    try:
        with os.fdopen(fd, 'wb') as file:
            if os.name != 'nt':
                if image is not None:
                    st = pending.stat()
                    if (st.st_uid, st.st_gid) != image[4][:2]:
                        os.fchown(file.fileno(), *image[4][:2])
                os.fchmod(file.fileno(), mode)
            file.write(raw); file.flush(); os.fsync(file.fileno())
        staged = host_image(pending)
        def commit():
            if host_image(path) != image or host_image(pending) != staged:
                raise HostConfigChanged('Host configuration concurrently changed')
            if image is None:
                if os.name == 'nt': os.rename(pending, path)
                else: os.link(pending, path); pending.unlink()
            else: atomic_replace(pending, path)
            sync_directory(path.parent)
        yield commit
    finally:
        pending.unlink(missing_ok=True)


def host_atomic(path, raw, mode, expected):
    with host_stage(path, raw, mode, expected) as commit: commit()


def host_plan(payload):
    """Bounded data images for nonsecret host files; journal stays private."""
    rows = {}
    for path, target in payload.items():
        image = host_image(path)
        before = image[0] if image else None
        if target is not None and (not isinstance(target, bytes) or len(target) > 1048576):
            raise ValueError('Bounded host target required')
        rows[str(path)] = {'before': base64.b64encode(before).decode() if before is not None else None,
                           'target': base64.b64encode(target).decode() if target is not None else None,
                           'before_sha': sha(before) if before is not None else None,
                           'target_sha': sha(target) if target is not None else None,
                           'mode': path.stat().st_mode & 0o777 if image else 0o600}
    return rows


def host_row(path, row):
    keys = {'before', 'target', 'before_sha', 'target_sha', 'mode'}
    if not keys <= set(row) or set(row)-keys-{'identity', 'rollback'} or type(row['mode']) is not int or row['mode'] & ~0o777:
        raise ValueError('Host journal changed')
    before, target = (base64.b64decode(row[k], validate=True) if row[k] is not None else None for k in ('before', 'target'))
    if any(v is not None and len(v)>1048576 for v in (before,target)) or (sha(before) if before is not None else None)!=row['before_sha'] or (sha(target) if target is not None else None)!=row['target_sha']:
        raise ValueError('Host journal images changed')
    image = host_image(path); current = image[0] if image else None
    identity = row.get('identity')
    if identity is not None:
        if not isinstance(identity,dict) or set(identity)!={'device','inode'} or any(type(v) is not int or v<0 for v in identity.values()) or image is None or image[1:3]!=(identity['device'],identity['inode']) or before is None or target is None:
            raise ValueError('Host recovery object changed')
        intent = row.get('rollback')
        if intent is not None:
            if not isinstance(intent,dict) or set(intent)!={'data','sha'}: raise ValueError('Host rollback intent changed')
            torn = base64.b64decode(intent['data'],validate=True)
            if sha(torn)!=intent['sha'] or partial_prefix(before,target,torn) is None or partial_prefix(torn,before,current) is None:
                raise ValueError('Outside host edit preserved')
        elif partial_prefix(before,target,current) is None: raise ValueError('Outside host edit preserved')
    elif current not in (before,target) or 'rollback' in row:
        raise ValueError('Outside host edit preserved')
    return before,target,current


def partial_prefix(before, after, current):
    if current == after: return len(after)
    offset = 0
    for old, new in zip(current, after):
        if old != new: break
        offset += 1
    if len(current) == max(len(before),offset) and current == after[:offset]+before[offset:]: return offset
    return None


def host_apply(rows, persist):
    """Persist exact intent before data writes, including Windows file identity."""
    from contextlib import ExitStack
    with ExitStack() as staging:
        commits = {}
        for name,row in rows.items():
            path=Path(name)
            if (host_image(path) or (None,))[0] != (base64.b64decode(row['before'],validate=True) if row['before'] is not None else None):
                raise HostConfigChanged('Host configuration concurrently changed')
            before,target,current=host_row(path,row)
            if current!=before: raise ValueError('Host preimage changed')
            if target is not None:
                commit=staging.enter_context(host_stage(path,target,row['mode'],before))
                if hasattr(commit,'inplace_identity'): row['identity']=commit.inplace_identity
                commits[name]=commit
        persist()
        for name,row in rows.items():
            if name in commits: commits[name]()
            else:
                path=Path(name)
                if (host_image(path) or (None,))[0] != (base64.b64decode(row['before']) if row['before'] is not None else None):
                    raise ValueError('Host removal preimage changed')
                path.unlink(missing_ok=True)


def host_restore(rows, persist):
    # Callers validate their exact permitted path set and image hashes first.
    for name,row in rows.items(): host_row(Path(name),row)
    for name,row in rows.items():
        path=Path(name); before,target,current=host_row(path,row)
        if current==before: continue
        if before is None: path.unlink(missing_ok=True)
        else:
            if 'identity' in row and 'rollback' not in row:
                row['rollback']={'data':base64.b64encode(current).decode(),'sha':sha(current)}
                persist()
            host_atomic(path,before,row['mode'],current)


def connect(home, profile_path=None, apply=False, approved_digest=None):
    home=Path(home).resolve();state=home/'.config/agent-brain-client';registration=state/'codex-mcp.json'
    if os.sys.platform!='darwin':return {'status':'adapter-review-required','native_changed':False,'next_action':'Verify actual Harness loading and prepare its adapter; only Mac Codex is wired here.'}
    native=Path(os.environ.get('CODEX_HOME',str(home/'.codex'))).expanduser().resolve()
    config=native/'config.toml';agents=native/'AGENTS.md';legacy=home/'.config/agent-brain/device.json'
    for p in (registration,config,agents,state,state.parent):
        if p.is_symlink():return {'status':'local-state-review-required','native_changed':False,'next_action':'Preserve linked native/client state and inspect ownership.'}
    override=native/'AGENTS.override.md'
    if override.exists() and override.read_text(encoding='utf-8').strip():return {'status':'native-repair-review-required','native_changed':False,'next_action':'Existing override shadows managed instructions; review before installation or reuse.'}
    cfg=tomllib.loads(config.read_text(encoding='utf-8')) if config.exists() else {}
    if profile_path is None and (state/'service-profile.json').exists():profile_path=state/'service-profile.json'
    candidate,mcp=profile(profile_path) if profile_path else (None,None)
    if registration.exists():
        registered=json.loads(registration.read_text(encoding='utf-8'));installed=cfg.get('mcp_servers',{}).get(NAME)
        if not isinstance(registered,dict) or registered.get('schema')!=1 or registered.get('data_mode') not in ('synthetic-only','personal-explicit') or registered.get('real_data_authorized') is not (registered.get('data_mode')=='personal-explicit') or registered.get('native_mcp_name')!=NAME or not installed:
            return {'status':'local-state-review-required','native_changed':False,'next_action':'Preserve unsupported client registration.'}
        if 'mcp_sha256' in registered:
            if legacy.exists():return {'status':'migration-review-required','native_changed':False,'next_action':'A later legacy registration overlaps this client; preserve both and review migration.'}
            text=agents.read_text(encoding='utf-8') if agents.is_file() else ''
            managed=MANAGED if registered['data_mode']=='synthetic-only' else PERSONAL_MANAGED
            valid=sha(json.dumps(installed,sort_keys=True).encode())==registered['mcp_sha256'] and text.count(BEGIN)==text.count(END)==1 and BLOCK.findall(text)==[managed]
        else:
            receipt_path=Path(registered.get('migration_receipt',''))
            valid=False
            if receipt_path.is_file() and not receipt_path.is_symlink() and not receipt_path.stat().st_mode&0o077:
                receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
                valid=receipt.get('installation_complete') is True and receipt.get('native_registered') is True and receipt.get('new_registration_path')==str(registration)
                owned={str(config),str(agents),str(native/'hooks.json'),str(home/'.config/agent-brain/codex/permissions-policy.json'),str(home/'.config/agent-brain/codex/integrity.json'),str(registration)}
                valid=valid and set(receipt.get('after',{}))==owned and all(Path(p).is_file() and not Path(p).is_symlink() and (migration_config_matches(config,config.read_bytes(),expected,state,receipt) if p==str(config) else sha(Path(p).read_bytes())==expected) for p,expected in receipt['after'].items())
        if not valid:return {'status':'native-repair-review-required','native_changed':False,'next_action':'Preserve drifted native files and review the owned migration receipt.'}
        if mcp is not None and (installed!=mcp or registered['data_mode']!=candidate['data_mode']):return {'status':'service-change-review-required','native_changed':False,'next_action':'Review changed SSH identity, fixed command, data mode or destination.'}
        updates=probe(installed)
        return {'status':'native-client-registered','native_changed':False,'protocol_checked':True,'model_loaded':'unknown','legacy_preserved':legacy.exists(),'updates':updates,'next_action':'Reuse this '+service_label(registered['data_mode'])+'. A new release source or runtime bootstrap requires its own pinned approval.'}
    if legacy.exists():return {'status':'migration-review-required','native_changed':False,'next_action':'Prepare an exact legacy migration with backups; preserve existing hooks, bindings and permissions.'}
    if candidate is None:return {'status':'server-authorization-required','native_changed':False,'next_action':'Locate the approved private SSH service profile. Do not send keys or tokens to chat.'}
    if NAME in cfg.get('mcp_servers',{}) or (agents.exists() and (BEGIN in agents.read_text(encoding='utf-8') or END in agents.read_text(encoding='utf-8'))):
        return {'status':'native-repair-review-required','native_changed':False,'next_action':'Existing unowned MCP/managed block preserved; inspect its registration.'}
    updates=probe(mcp)
    config_raw=config.read_bytes() if config.exists() else b'';agents_raw=agents.read_bytes() if agents.exists() else b''
    snippet='\n# '+service_label(candidate['data_mode'])+'\n[mcp_servers.'+NAME+']\n'
    for key,value in mcp.items():snippet+=key+' = '+json.dumps(value,ensure_ascii=False)+'\n'
    managed=MANAGED if candidate['data_mode']=='synthetic-only' else PERSONAL_MANAGED
    new_config=config_raw+b'\n'+snippet.encode();new_agents=agents_raw+(b'\n' if agents_raw and not agents_raw.endswith(b'\n') else b'')+managed.encode()
    expected=copy.deepcopy(cfg);expected.setdefault('mcp_servers',{})[NAME]=mcp
    if tomllib.loads(new_config.decode())!=expected:raise ValueError('Native TOML scope changed')
    plan=sha(json.dumps({'profile':candidate,'before':{str(config):sha(config_raw),str(agents):sha(agents_raw)},'after':{str(config):sha(new_config),str(agents):sha(new_agents)}},sort_keys=True).encode())
    if not apply:return {'status':'ready-to-register','native_changed':False,'plan_sha256':plan,'updates':updates,'next_action':'Apply this exact native plan only within authorization; no credentials, hooks or background tasks are installed.'}
    if approved_digest!=plan:return {'status':'plan-review-required','native_changed':False,'plan_sha256':plan,'next_action':'Approve the exact current native plan digest before applying.'}
    value={'schema':1,'native_mcp_name':NAME,'data_mode':candidate['data_mode'],'real_data_authorized':candidate['data_mode']=='personal-explicit','production_proactive_memory':False,'mcp_sha256':sha(json.dumps(mcp,sort_keys=True).encode()),'platform':candidate['platform'],'model_loaded':'unknown'}
    payload={config:new_config,agents:new_agents,registration:(json.dumps(value,indent=2)+'\n').encode()}
    receipt=state/'native-install-receipt.json'
    before={str(p):{'data':base64.b64encode(p.read_bytes()).decode(),'mode':p.stat().st_mode&0o777} if p.exists() else None for p in payload}
    after={str(p):sha(raw) for p,raw in payload.items()};journal={'schema':1,'phase':'prepared','plan_sha256':plan,'before':before,'after':after}
    if receipt.is_symlink():raise ValueError('Linked transaction receipt preserved')
    if receipt.exists():
        previous=json.loads(receipt.read_text(encoding='utf-8'))
        if previous.get('phase')!='rolled-back' or previous.get('plan_sha256')!=plan or previous.get('before')!=before:
            return {'status':'native-repair-review-required','native_changed':False,'next_action':'Existing transaction receipt preserved; inspect before retry.'}
    atomic(receipt,(json.dumps(journal)+'\n').encode(),0o600)
    written=[]
    try:
        for p,raw in payload.items():
            old=before[str(p)]
            if p.is_symlink() or (p.read_bytes() if p.exists() else None)!=(base64.b64decode(old['data']) if old else None):raise ValueError('Native preimage changed')
            atomic(p,raw,old['mode'] if old else 0o600);written.append(p)
        journal['phase']='complete';atomic(receipt,(json.dumps(journal)+'\n').encode(),0o600)
    except Exception:
        # Check all postimages before restoring anything; preserve external drift.
        if any(p.is_symlink() or not p.exists() or sha(p.read_bytes())!=after[str(p)] for p in written):raise ValueError('Native rollback conflict preserved') from None
        for p in reversed(written):
            old=before[str(p)]
            if old:atomic(p,base64.b64decode(old['data']),old['mode'])
            else:p.unlink()
        journal['phase']='rolled-back';atomic(receipt,(json.dumps(journal)+'\n').encode(),0o600);raise
    return {'status':'native-client-registered','native_changed':True,'protocol_checked':True,'model_loaded':'unknown','updates':updates,'next_action':'Use the registered '+service_label(candidate['data_mode'])+'; configuration and protocol checks completed. Model loading remains unknown.'}
