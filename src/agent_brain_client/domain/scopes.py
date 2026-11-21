"""Credential-free source routing contract, shared as independently shipped code."""

import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
import re
import unicodedata


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def ordinary_drive_path(value):
    """Strip only an extended local drive prefix with an equivalent DOS path."""
    if not isinstance(value, str) or not value.startswith("\\\\?\\"):
        return value
    candidate = value[4:]
    if not re.match(r"^[A-Za-z]:\\", candidate):
        return value

    # The extended syntax skips normal Win32 path processing. A segment must
    # mean the same thing under both spellings before we remove the prefix.
    tail = candidate[3:]
    for part in tail.removesuffix("\\").split("\\") if tail else ():
        if (
            not part
            or part.startswith(" ")
            or part.endswith((" ", "."))
            or re.search(r'[<>:"/|?*\x00-\x1f]', part)
            or re.fullmatch(
                r"CON(?:IN\$|OUT\$)?|PRN|AUX|NUL|(?:COM|LPT)[0-9¹²³]",
                part.partition(".")[0].rstrip(" ").upper(),
            )
        ):
            return value
    return candidate


def path(value):
    if not isinstance(value, str) or any(c in value for c in ("\x00", "\r", "\n")):
        raise ValueError("Absolute source metadata required")
    value = unicodedata.normalize("NFC", value)
    windows = PureWindowsPath(value)
    if windows.drive:
        # Local absolute drive paths only: no UNC, device namespace, ADS or
        # drive-relative syntax. A Windows drive cannot match a POSIX root.
        if (
            not re.match(r"^[A-Za-z]:[\\/]", value)
            or not windows.is_absolute()
            or ".." in windows.parts
            or any(":" in p for p in windows.parts[1:])
        ):
            raise ValueError("Absolute local Windows source required")
        return windows.as_posix().casefold()
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or ".." in PurePosixPath(value).parts
    ):
        raise ValueError("Absolute source metadata required")
    return str(PurePosixPath(value))


def within(value, root):
    a, b = path(value).casefold(), path(root).casefold()
    return a == b or a.startswith(b.rstrip("/") + "/")


def validate(scope):
    if not isinstance(scope, dict) or set(scope) - {"all_projects", "all_non_projects"} != {
        "schema",
        "profile_id",
        "include_roots",
        "exclude_roots",
        "non_project_roots",
        "recall_personal",
    }:
        raise ValueError("Exact global source policy required")
    if (
        scope["schema"] != 1
        or not re.fullmatch(r"[a-f0-9]{32}", str(scope["profile_id"]))
        or type(scope["recall_personal"]) is not bool
    ):
        raise ValueError("Explicit global source identity required")
    if type(scope.get("all_projects", False)) is not bool:
        raise ValueError("Boolean all-projects selection required")
    if type(scope.get("all_non_projects", False)) is not bool:
        raise ValueError("Boolean all-non-projects selection required")
    roots = scope["include_roots"]
    if not isinstance(roots, list) or len(roots) > 128:
        raise ValueError("Bounded source rules required")
    seen = set()
    for rule in roots:
        if (
            not isinstance(rule, dict)
            or set(rule) != {"root", "sharing"}
            or rule["sharing"] not in ("personal", "project")
        ):
            raise ValueError("Personal or project sharing must be explicit")
        key = path(rule["root"]).casefold()
        if key in seen:
            raise ValueError("Duplicate source rule")
        seen.add(key)
    for key in ("exclude_roots", "non_project_roots"):
        if not isinstance(scope[key], list) or len(scope[key]) > 128:
            raise ValueError("Bounded directory rules required")
        for root in scope[key]:
            path(root)
    return scope


def origin(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"kind", "cwd", "project_root", "repository_root"}
        or value["kind"] not in ("project", "non-project")
    ):
        raise ValueError("Explicit local origin metadata required")
    path(value["cwd"])
    if value["kind"] == "project":
        path(value["project_root"])
        path(value["repository_root"])
        if not within(value["cwd"], value["project_root"]):
            raise ValueError("Working directory outside project")
    elif value["project_root"] is not None or value["repository_root"] is not None:
        raise ValueError("Non-project origin must not claim a repository")
    return value


def _excluded(scope, value):
    paths = [
        value[k]
        for k in ("cwd", "project_root", "repository_root")
        if value[k] is not None
    ]
    return any(within(p, denied) for p in paths for denied in scope["exclude_roots"])


def has_origins(scope):
    return bool(
        scope.get("all_projects")
        or scope.get("all_non_projects")
        or scope["include_roots"]
        or scope["non_project_roots"]
    )


def ordinary_default(scope):
    """Recognize the old active default without any ordinary-directory choice."""
    return (
        scope.get("all_projects") is True
        and "all_non_projects" not in scope
        and not scope["non_project_roots"]
        and scope["recall_personal"]
    )


def select(scope, value, *, recall=False):
    validate(scope)
    origin(value)
    if _excluded(scope, value):
        return None
    if value["kind"] == "non-project":
        # Missing project markers must not turn an isolated directory personal.
        if (recall and not scope["recall_personal"]) or any(
            rule["sharing"] == "project" and within(value["cwd"], rule["root"])
            for rule in scope["include_roots"]
        ):
            return None
        # Existing default all-project policies already authorize personal recall.
        # Their stored capture approval still requires an explicit ordinary scope.
        personal_recall = recall and ordinary_default(scope)
        if (
            personal_recall
            or scope.get("all_non_projects")
            or any(within(value["cwd"], p) for p in scope["non_project_roots"])
        ):
            return {"sharing": "personal", "root": None}
        return None
    matches = [
        r for r in scope["include_roots"] if within(value["project_root"], r["root"])
    ]
    if matches:
        selected = max(matches, key=lambda r: len(path(r["root"])))
        if recall and not scope["recall_personal"] and selected["sharing"] == "personal":
            return None
        return selected
    if scope.get("all_projects") and (not recall or scope["recall_personal"]):
        return {"sharing": "personal", "root": None}
    return None


def source_id(scope, value, sharing):
    if sharing == "personal":
        return scope["profile_id"]
    # Worktrees share their repository namespace; directory exclusions still
    # check both the checkout and repository root before this decision.
    key = path(
        value["repository_root"] or value["project_root"] or value["cwd"]
    ).casefold()
    return digest([scope["profile_id"], "project", key])[:32]


def namespace(actor, source):
    return "abp-" + digest([actor, source])[:32]


def restricted(text):
    """Use the same explicit-intent boundary for capture and recall."""
    from agent_brain_client.domain.contract import explicit_sharing_restriction

    return explicit_sharing_restriction(text)


def source_context(origins, current):
    """Location applicability is separate from searchable memory content."""
    origin(current)
    rows = {digest(origin(item)): item for item in origins}
    if len(rows) > 32:
        raise ValueError("Bounded verified origin context required")

    def key(item):
        return (item["kind"], path(item["repository_root"] or item["cwd"]).casefold())

    matches = [key(item) == key(current) for item in rows.values()]
    relation = (
        "unknown-source"
        if not rows
        else "current-source"
        if all(matches)
        else "other-source"
        if not any(matches)
        else "mixed-source"
    )
    return {
        "origins": [rows[k] for k in sorted(rows)],
        "relation": relation,
        "use": "reference-only",
        "global_rule": False,
    }
