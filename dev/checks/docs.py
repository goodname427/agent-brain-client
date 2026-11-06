"""Check repository Markdown links and anchors without network or dependencies."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    _ENTRY_ROOT = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(_ENTRY_ROOT / "src"), str(_ENTRY_ROOT)]


import html
import json
import posixpath
import re
import unicodedata
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
LINK = re.compile(r'!?\[[^\]\n]*\]\(<?([^\s)>]+)>?(?:\s+["\'][^\n]*?["\'])?\)')
REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?([^\s>]+)>?", re.M)
HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.M)
ANCHOR = re.compile(r'<[^>]+\b(?:id|name)=["\']([^"\']+)["\'][^>]*>')


def prose(source):
    """Exclude fenced examples; they are not rendered navigation."""
    lines, fence, width = [], None, 0
    for line in source.splitlines():
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if match:
            token = match[1]
            if fence is None:
                fence, width = token[0], len(token)
            elif token[0] == fence and len(token) >= width:
                fence = None
            lines.append("")
        else:
            lines.append(line if fence is None else "")
    return "\n".join(lines)


def anchors(source):
    source = prose(source)
    result, counts = set(ANCHOR.findall(source)), {}
    for heading in HEADING.findall(source):
        heading = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", heading)
        heading = html.unescape(re.sub(r"<[^>]+>", "", heading)).lower()
        slug = "".join(
            c for c in heading if c in " -_" or unicodedata.category(c)[0] in "LNM"
        )
        slug = slug.replace(" ", "-")
        number = counts.get(slug, 0)
        counts[slug] = number + 1
        result.add(slug + (f"-{number}" if number else ""))
    return result


def local_target(name, url):
    split = urlsplit(html.unescape(url))
    if split.scheme or split.netloc:
        return None
    path = unquote(split.path)
    if path.startswith("/"):
        raise ValueError(f"{name}: repository-relative link must not start with /")
    target = (
        posixpath.normpath(posixpath.join(posixpath.dirname(name), path))
        if path
        else name
    )
    if target == ".." or target.startswith("../"):
        raise ValueError(f"{name}: link escapes repository: {url}")
    return target, unquote(split.fragment)


def check(root=ROOT):
    names = sorted(p.relative_to(root).as_posix() for p in root.glob("*.md"))
    names += sorted(
        p.relative_to(root).as_posix() for p in (root / "docs").rglob("*.md")
    )
    sources = {name: (root / name).read_text(encoding="utf-8") for name in names}
    known_anchors = {name: anchors(source) for name, source in sources.items()}
    count = 0
    for name, source in sources.items():
        rendered = prose(source)
        # Link-shaped inline-code examples are not navigation either.
        rendered = re.sub(r"`+[^`\n]*`+", "", rendered)
        for expression in (LINK, REFERENCE):
            for match in expression.finditer(rendered):
                target = local_target(name, match[1])
                if target is None:
                    continue
                dest, fragment = target
                path = root / dest
                if not path.exists():
                    raise ValueError(f"{name}: missing relative target {match[1]}")
                if path.is_symlink():
                    raise ValueError(f"{name}: linked documentation target {dest}")
                if (
                    fragment
                    and dest in known_anchors
                    and fragment not in known_anchors[dest]
                ):
                    raise ValueError(f"{name}: missing anchor {match[1]}")
                count += 1
    return {
        "status": "documentation-checked",
        "markdown_files": len(names),
        "local_links": count,
        "external_urls_checked": False,
    }


if __name__ == "__main__":
    try:
        print(json.dumps(check()))
    except (ValueError, KeyError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
