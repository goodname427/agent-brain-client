"""Offline documentation links, legacy entry points and fixed-data contracts."""
import hashlib
import html
import json
import posixpath
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r'!?\[[^\]\n]*\]\(<?([^\s)>]+)>?(?:\s+["\'][^\n]*?["\'])?\)')
REFERENCE = re.compile(r'^\s{0,3}\[[^\]]+\]:\s*<?([^\s>]+)>?', re.M)
HEADING = re.compile(r'^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$', re.M)
ANCHOR = re.compile(r'<[^>]+\b(?:id|name)=["\']([^"\']+)["\'][^>]*>')
MARKER = '<!-- historical-body -->\n'


def prose(source):
    """Exclude fenced examples; they are not rendered navigation."""
    lines, fence, width = [], None, 0
    for line in source.splitlines():
        match = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if match:
            token = match[1]
            if fence is None:
                fence, width = token[0], len(token)
            elif token[0] == fence and len(token) >= width:
                fence = None
            lines.append('')
        else:
            lines.append(line if fence is None else '')
    return '\n'.join(lines)


def anchors(source):
    source = prose(source)
    result, counts = set(ANCHOR.findall(source)), {}
    for heading in HEADING.findall(source):
        heading = re.sub(r'!?\[([^\]]+)\]\([^)]*\)', r'\1', heading)
        heading = html.unescape(re.sub(r'<[^>]+>', '', heading)).lower()
        slug = ''.join(c for c in heading if c in ' -_' or unicodedata.category(c)[0] in 'LNM')
        slug = slug.replace(' ', '-')
        number = counts.get(slug, 0)
        counts[slug] = number + 1
        result.add(slug + (f'-{number}' if number else ''))
    return result


def local_target(name, url):
    split = urlsplit(html.unescape(url))
    if split.scheme or split.netloc:
        return None
    path = unquote(split.path)
    if path.startswith('/'):
        raise ValueError(f'{name}: repository-relative link must not start with /')
    target = posixpath.normpath(posixpath.join(posixpath.dirname(name), path)) if path else name
    if target == '..' or target.startswith('../'):
        raise ValueError(f'{name}: link escapes repository: {url}')
    return target, unquote(split.fragment)


def normalize_body(name, source):
    if MARKER not in source:
        raise ValueError(f'{name}: historical full-text marker missing')
    body = source.split(MARKER, 1)[1]
    def normalized(match):
        target = local_target(name, match[1])
        return match[0].replace(match[1], target[0] + ('#' + target[1] if target[1] else '')) if target else match[0]
    return LINK.sub(normalized, body)


def check():
    names = sorted(p.relative_to(ROOT).as_posix() for p in ROOT.glob('*.md'))
    names += sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / 'docs').rglob('*.md'))
    sources = {name: (ROOT / name).read_text(encoding='utf-8') for name in names}
    known_anchors = {name: anchors(source) for name, source in sources.items()}
    graph, count = {name: set() for name in names}, 0
    for name, source in sources.items():
        rendered = prose(source)
        # Link-shaped inline-code examples are not navigation either.
        rendered = re.sub(r'`+[^`\n]*`+', '', rendered)
        for expression in (LINK, REFERENCE):
            for match in expression.finditer(rendered):
                target = local_target(name, match[1])
                if target is None:
                    continue
                dest, fragment = target
                path = ROOT / dest
                if not path.exists():
                    raise ValueError(f'{name}: missing relative target {match[1]}')
                if path.is_symlink():
                    raise ValueError(f'{name}: linked documentation target {dest}')
                if fragment and dest in known_anchors and fragment not in known_anchors[dest]:
                    raise ValueError(f'{name}: missing anchor {match[1]}')
                if dest in sources:
                    graph[name].add(dest)
                count += 1
    contract = json.loads((ROOT / 'docs/documentation-contract.json').read_text())
    for name in ['README.md', 'AGENTS.md', 'BOOTSTRAP.md', *contract['legacy_markdown']]:
        if name not in sources or len(sources[name]) < 200:
            raise ValueError(f'{name}: missing complete legacy entry')
    for old, archived in contract['archive_mirrors'].items():
        if normalize_body(old, sources[old]) != normalize_body(archived, sources[archived]):
            raise ValueError(f'{old}: archive and compatibility full text differ')
    for name, expected in contract['fixed_data_sha256'].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f'{name}: fixed data contract changed; requires separate review')
        json.loads((ROOT / name).read_bytes())
    for name, required in contract['legacy_anchors'].items():
        if not set(required) <= known_anchors[name]:
            raise ValueError(f'{name}: legacy title anchor removed')
    reached, pending = set(), ['docs/README.md']
    while pending:
        name = pending.pop()
        if name not in reached:
            reached.add(name)
            pending.extend(graph[name] - reached)
    missing = sorted(set(names) - reached)
    if missing:
        raise ValueError('Documentation missing from index: ' + ', '.join(missing))
    return {'status': 'documentation-checked', 'markdown_files': len(names),
            'local_links': count, 'legacy_entries': len(contract['legacy_markdown']),
            'archive_mirrors': len(contract['archive_mirrors']),
            'fixed_data_contracts': len(contract['fixed_data_sha256']),
            'external_urls_checked': False}


if __name__ == '__main__':
    try:
        print(json.dumps(check()))
    except (ValueError, KeyError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
