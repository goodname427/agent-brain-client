"""Lossless TOML subtree editing, independent of a Harness."""

import copy
import json
import re
import tomllib

def toml_string(value):
    # JSON escapes C0 controls; TOML additionally forbids a literal DEL. Keep
    # Unicode scalars literal so non-BMP keys never become surrogate escapes.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", r"\u007f")


def toml_literal(value):
    """Encode a parsed TOML value, including values in a shared inline table."""
    import datetime
    import math

    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(
                toml_string(k) + " = " + toml_literal(v)
                for k, v in sorted(value.items())
            )
            + "}"
        )
    if isinstance(value, list):
        return "[" + ", ".join(map(toml_literal, value)) + "]"
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")
    return json.dumps(value, ensure_ascii=False)


def toml_equal(left, right):
    """TOML value equality keeps types distinct and treats NaN as itself."""
    import math

    if type(left) is not type(right):
        return False
    if isinstance(left, float) and math.isnan(left) and math.isnan(right):
        return True
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            toml_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            toml_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def toml_key(key):
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else toml_string(key)


def toml_statements(text):
    """Yield source spans and semantic paths, without interpreting string data.

    tomllib is the syntax authority. A statement ends only when it parses, so
    header/marker-looking lines in multiline strings and arrays remain data.
    Paths include array-table indices; quoted and dotted keys are equivalent.
    """
    tomllib.loads(text)
    context, prefix, pending, start, offset = (), "", "", 0, 0
    # TOML line breaks are LF/CRLF. Unicode separators can be ordinary comment
    # or string data and must not expose a spurious header or marker line.
    for line in re.findall(r"[^\n]*\n|[^\n]+$", text):
        pending += line
        offset += len(line)
        try:
            parsed = tomllib.loads(pending)
        except tomllib.TOMLDecodeError:
            continue
        stripped = pending.lstrip()
        kind, path = "comment", None
        if parsed:
            if stripped.startswith("["):
                kind = "header"
                tree = parsed
                names = []
                while isinstance(tree, (dict, list)) and tree:
                    if isinstance(tree, list):
                        tree = tree[-1]
                    else:
                        key, tree = next(iter(tree.items()))
                        names.append(key)
                # Resolve each array table against this exact source prefix.
                tree = tomllib.loads(prefix + pending)
                resolved = []
                for name in names:
                    tree = tree[name]
                    resolved.append(name)
                    if isinstance(tree, list):
                        resolved.append(len(tree) - 1)
                        tree = tree[-1]
                context = path = tuple(resolved)
            else:
                kind = "value"
                # Locate '=' outside a quoted key; parsing the LHS with a
                # sentinel recovers its path even if its value is a dictionary.
                quote, escaped = None, False
                for index, char in enumerate(pending):
                    if escaped:
                        escaped = False
                        continue
                    if quote:
                        if char == "\\" and quote == '"':
                            escaped = True
                        elif char == quote:
                            quote = None
                    elif char in ('"', "'"):
                        quote = char
                    elif char == "=":
                        break
                tree = tomllib.loads(pending[:index] + "=0")
                names = []
                while isinstance(tree, dict):
                    name, tree = next(iter(tree.items()))
                    names.append(name)
                path = context + tuple(names)
        yield start, offset, kind, path
        prefix += pending
        pending, start = "", offset
    if pending:
        raise ValueError("Incomplete TOML statement")


def toml_get(value, path):
    for key in path:
        value = value[key]
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
            if any(path[: len(target)] == target for target in paths):
                continue
            descendants = [
                target[len(path) :] for target in paths if target[: len(path)] == path
            ]
            if descendants and kind == "value":
                value = copy.deepcopy(toml_get(config, path))
                empty_assignment = False
                # Remove higher array indices before lower ones.
                for target in sorted(descendants, reverse=True):
                    parent = toml_get(value, target[:-1])
                    del parent[target[-1]]
                    if isinstance(target[-1], int) and not parent:
                        if len(target) == 1:
                            empty_assignment = True
                            break
                        del toml_get(value, target[:-2])[target[-2]]
                if empty_assignment:
                    continue
                # Reuse the exact quoted/dotted LHS, found by a sentinel parse.
                for index, char in enumerate(source):
                    if char != "=":
                        continue
                    try:
                        tomllib.loads(source[:index] + "=0")
                    except tomllib.TOMLDecodeError:
                        continue
                    source = (
                        source[: index + 1]
                        + " "
                        + toml_literal(value)
                        + ("\r\n" if source.endswith("\r\n") else "\n")
                    )
                    break
        result.append(source)
    return "".join(result)


def toml_replace(text, path, value):
    """Replace one tree, including when embedded in a shared inline table."""
    config = tomllib.loads(text)
    for start, end, kind, current in toml_statements(text):
        if kind != "value" or path[: len(current)] != current:
            continue
        replacement = copy.deepcopy(toml_get(config, current))
        if current == path:
            replacement = value
        else:
            toml_get(replacement, path[len(current) : -1])[path[-1]] = value
        source = text[start:end]
        for index, char in enumerate(source):
            if char != "=":
                continue
            try:
                tomllib.loads(source[:index] + "=0")
            except tomllib.TOMLDecodeError:
                continue
            newline = "\r\n" if source.endswith("\r\n") else "\n"
            return (
                text[:start]
                + source[: index + 1]
                + " "
                + toml_literal(replacement)
                + newline
                + text[end:]
            )
    outside = toml_remove(text, [path])
    header = ".".join(toml_key(key) for key in path)
    groups = value if isinstance(value, list) else [value]
    snippet = ""
    for group in groups:
        snippet += (
            "[[" + header + "]]" if isinstance(value, list) else "[" + header + "]"
        ) + "\n"
        snippet += "".join(
            toml_key(key) + " = " + toml_literal(item) + "\n"
            for key, item in sorted(group.items())
        )
    return outside.rstrip() + "\n\n" + snippet
