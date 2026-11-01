"""Credential-free source routing contract, shared as independently shipped code."""
import hashlib
import json
from pathlib import PurePosixPath
import re
import unicodedata


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def path(value):
    if not isinstance(value, str) or not value.startswith('/') or '\x00' in value or '..' in PurePosixPath(value).parts:
        raise ValueError('Absolute source metadata required')
    return str(PurePosixPath(unicodedata.normalize('NFC', value)))


def within(value, root):
    a, b = path(value).casefold(), path(root).casefold()
    return a == b or a.startswith(b.rstrip('/') + '/')


def validate(scope):
    if not isinstance(scope, dict) or set(scope) != {'schema', 'profile_id', 'include_roots', 'exclude_roots', 'non_project_roots', 'recall_personal'}:
        raise ValueError('Exact global source policy required')
    if scope['schema'] != 1 or not re.fullmatch(r'[a-f0-9]{32}', str(scope['profile_id'])) or type(scope['recall_personal']) is not bool:
        raise ValueError('Explicit global source identity required')
    roots = scope['include_roots']
    if not isinstance(roots, list) or len(roots) > 128:
        raise ValueError('Bounded source rules required')
    seen = set()
    for rule in roots:
        if not isinstance(rule, dict) or set(rule) != {'root', 'sharing'} or rule['sharing'] not in ('personal', 'project'):
            raise ValueError('Personal or project sharing must be explicit')
        key = path(rule['root']).casefold()
        if key in seen:
            raise ValueError('Duplicate source rule')
        seen.add(key)
    for key in ('exclude_roots', 'non_project_roots'):
        if not isinstance(scope[key], list) or len(scope[key]) > 128:
            raise ValueError('Bounded directory rules required')
        for root in scope[key]:
            path(root)
    return scope


def origin(value):
    if not isinstance(value, dict) or set(value) != {'kind', 'cwd', 'project_root', 'repository_root'} or value['kind'] not in ('project', 'non-project'):
        raise ValueError('Explicit local origin metadata required')
    path(value['cwd'])
    if value['kind'] == 'project':
        path(value['project_root']); path(value['repository_root'])
        if not within(value['cwd'], value['project_root']):
            raise ValueError('Working directory outside project')
    elif value['project_root'] is not None or value['repository_root'] is not None:
        raise ValueError('Non-project origin must not claim a repository')
    return value


def select(scope, value):
    validate(scope); origin(value)
    paths = [p for p in value.values() if isinstance(p, str) and p.startswith('/')]
    if any(within(p, denied) for p in paths for denied in scope['exclude_roots']):
        return None
    if value['kind'] == 'non-project':
        return {'sharing': 'personal', 'root': None} if any(within(value['cwd'], p) for p in scope['non_project_roots']) else None
    matches = [r for r in scope['include_roots'] if within(value['project_root'], r['root'])]
    return max(matches, key=lambda r: len(path(r['root']))) if matches else None


def source_id(scope, value, sharing):
    if sharing == 'personal':
        return scope['profile_id']
    # Worktrees share their repository namespace; directory exclusions still
    # check both the checkout and repository root before this decision.
    key = path(value['repository_root'] or value['project_root'] or value['cwd']).casefold()
    return digest([scope['profile_id'], 'project', key])[:32]


def namespace(actor, source):
    return 'abp-' + digest([actor, source])[:32]


def restricted(text):
    """Explicit sharing restrictions; callers also run existing secret guards."""
    return isinstance(text, str) and bool(re.search(
        r"(?im)(?:^\s*\[(?:no[-_ ]?share|project[-_ ]?only|do[-_ ]?not[-_ ]?share)\]|禁止(?:跨项目)?共享|不要(?:跨项目)?共享|仅限(?:当前|本)项目|do not share (?:this|across projects)|keep (?:this )?project[- ]only)", text))


def source_context(origins, current):
    """Location applicability is separate from searchable memory content."""
    origin(current)
    rows = {digest(origin(item)): item for item in origins}
    if not rows or len(rows) > 32:
        raise ValueError('Bounded verified origin context required')
    def key(item):
        return (item['kind'], path(item['repository_root'] or item['cwd']).casefold())
    matches = [key(item) == key(current) for item in rows.values()]
    relation = 'current-source' if all(matches) else 'other-source' if not any(matches) else 'mixed-source'
    return {'origins': [rows[k] for k in sorted(rows)], 'relation': relation,
            'use': 'reference-only', 'global_rule': False}
