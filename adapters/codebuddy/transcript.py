"""Read only a CodeBuddy turn observed after installation, never scan history.

CLI uses the Hook's JSONL suffix after the prompt cursor. IDE uses the exact
generation's message IDs from its index.json, as implemented by CodeBuddy's
HistoryStorage/ConversationManager. Unsupported native formats remain visible.
"""
import json
import os
from pathlib import Path
import re
import stat

from runtime import canonical, digest, session_id, unique

LIMIT = 4 * 1048576


def native_path(home, payload):
    raw = payload.get('transcript_path')
    if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
        raise ValueError('Native CodeBuddy transcript path unavailable')
    path = canonical(raw)
    roots = (Path(home) / '.codebuddy', Path(home) / 'Library/Caches/genie-cache', Path(home) / 'AppData/Local/genie-cache')
    if not any(root in path.parents for root in roots):
        raise ValueError('Transcript outside CodeBuddy native storage')
    session = payload['session_id']
    if path.name == 'index.json':
        if path.parent.name != session or not payload.get('generation_id'):
            raise ValueError('IDE current generation binding required')
    elif path.suffix != '.jsonl' or path.stem != session:
        raise ValueError('Recognized current CodeBuddy session file required')
    return path


def open_native(path):
    path = canonical(path)
    handle = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    stream = os.fdopen(handle, 'rb')
    info = os.fstat(handle)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        stream.close()
        raise ValueError('Native transcript regular file required')
    return stream, info


def marker(home, payload):
    session = session_id(payload['session_id'])
    generation = payload.get('generation_id')
    if generation is not None and (not isinstance(generation, str) or not generation or len(generation) > 1024):
        raise ValueError('Native generation required')
    key = digest(json.dumps([session, generation], separators=(',', ':')).encode())
    return Path(home) / '.config/agent-brain-client/codebuddy/turns' / (key + '.json')


def observe(home, payload, core):
    from brain_lock import cache_directory, locked
    from global_memory_scope import restricted
    try:
        path = native_path(home, payload)
    except ValueError:
        path = None  # Current CLI can provide final text directly on Stop.
    file = marker(home, payload)
    cache_directory(file.parent)
    with locked(file.parent / 'turns.lock'):
        existing = core.old.private_json(file, cache=True) if file.exists() else None
        binding = {'session_id': payload['session_id'], 'generation_id': payload.get('generation_id'),
                   'path': str(path) if path else None, 'cwd': payload['cwd'], 'prompt_sha256': digest(payload['prompt'].encode())}
        # A stable generation can be redelivered. Older IDEs without one have
        # only a single active turn per session, and get a new cursor per prompt.
        if existing and payload.get('generation_id'):
            if existing['binding'] != binding:
                raise ValueError('Native turn binding changed')
            return
        try:
            if path is None:
                raise FileNotFoundError()
            stream, info = open_native(path)
            stream.close()
            identity, offset = [info.st_dev, info.st_ino], info.st_size
        except FileNotFoundError:
            identity, offset = None, 0
        text = payload['prompt']
        row = {'schema': 1, 'binding': binding, 'identity': identity, 'offset': offset,
               'excluded': bool(core.old.private_content(text) or restricted(text) or existing and not existing['complete'] and not payload.get('generation_id')), 'complete': False}
        core.old.write(file, row, cache=True)


def plain(message):
    if isinstance(message, str):
        try:
            message = json.loads(message, object_pairs_hook=unique)
        except ValueError:
            return message
    if not isinstance(message, dict):
        return ''
    content = message.get('content')
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    # Do not collect reasoning, tool arguments/results, attachments or images.
    return '\n'.join(part['text'] for part in content if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str))


def read_bound(path, *, offset=0, identity=None):
    with open_native(path)[0] as stream:
        info = os.fstat(stream.fileno())
        if identity is not None and [info.st_dev, info.st_ino] != identity or info.st_size < offset:
            raise ValueError('Native transcript rotated or truncated')
        if info.st_size - offset > LIMIT:
            raise ValueError('Current native turn exceeds read bound')
        stream.seek(offset)
        raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError('Native transcript exceeds bound')
        if canonical(path).stat().st_ino != info.st_ino:
            raise ValueError('Native transcript replaced during read')
        return raw


def assistant(home, payload, core):
    file = marker(home, payload)
    if not file.exists():
        raise ValueError('No observed future prompt; historical import refused')
    row = core.old.private_json(file, cache=True)
    binding = row['binding']
    if binding['session_id'] != payload['session_id'] or binding['generation_id'] != payload.get('generation_id') or binding['cwd'] != payload['cwd']:
        raise ValueError('Native Stop scope changed')
    if row['excluded'] or row['complete']:
        return [], row, file
    direct = payload.get('last_assistant_message')
    if isinstance(direct, str) and direct.strip():
        return [{'role': 'assistant', 'content': direct}], row, file
    path = native_path(home, payload)
    if binding['path'] != str(path):
        raise ValueError('Native transcript binding changed')
    texts = []
    if path.suffix == '.jsonl':
        raw = read_bound(path, offset=row['offset'], identity=row['identity'])
        if raw and not raw.endswith(b'\n'):
            raise ValueError('Native transcript still being written')
        for line in raw.splitlines():
            if not line.strip():
                continue
            item = json.loads(line, object_pairs_hook=unique)
            if not isinstance(item, dict) or item.get('type') != 'assistant':
                if isinstance(item, dict) and item.get('type') == 'user':
                    text = plain(item.get('message'))
                    if text.strip() and digest(text.encode()) != binding['prompt_sha256']:
                        raise ValueError('Overlapping native user turn; preserve cursor')
                continue
            if item.get('sessionId', payload['session_id']) != payload['session_id']:
                raise ValueError('JSONL session changed')
            text = plain(item.get('message'))
            if text.strip():
                texts.append(text)
    else:
        index = json.loads(read_bound(path), object_pairs_hook=unique)
        requests = [r for r in index.get('requests', []) if r.get('id') == payload['generation_id']]
        if len(requests) != 1:
            raise ValueError('IDE current generation unavailable')
        ids = requests[0].get('messages')
        if not isinstance(ids, list) or len(ids) > 1000 or any(not isinstance(mid, str) or not re.fullmatch('[A-Za-z0-9_-]{1,128}', mid) for mid in ids):
            raise ValueError('IDE generation message references invalid')
        metadata = {m['id']: m for m in index.get('messages', []) if isinstance(m, dict) and m.get('id') in ids and m.get('role') == 'assistant'}
        for mid in ids:
            if mid not in metadata:
                continue
            native = path.parent / 'messages' / (mid + '.json')
            item = json.loads(read_bound(native), object_pairs_hook=unique)
            if item.get('id') != mid or item.get('role') != 'assistant':
                raise ValueError('IDE current message identity changed')
            text = plain(item.get('message'))
            if text.strip():
                texts.append(text)
    return [{'role': 'assistant', 'content': text} for text in texts], row, file
