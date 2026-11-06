"""domain / authorization: one owner for each contract and rule."""

import re
from agent_brain_client.domain.operations import FIELDS
from agent_brain_client.domain.operations import HOOK_METHODS
from agent_brain_client.domain.content import TYPES


OWNER_SCOPE = "personal-owner"


def normalized_scopes(scopes):
    return {"put" if scope == "write" else scope for scope in scopes}


def permits_scope(scopes, method):
    if method in ("content_set_enabled", "content_put", "memory_event_submit"):
        return "put" in normalized_scopes(scopes)
    if method == "content_remove":
        return "remove" in scopes
    return "read" in scopes


def methods(scopes):
    return {name for name in FIELDS if permits_scope(scopes, name)}


def mcp_methods(scopes):
    return methods(scopes) - HOOK_METHODS


def personal_owner(ids):
    return (
        isinstance(ids, dict)
        and set(ids) == TYPES
        and all(value == OWNER_SCOPE for value in ids.values())
    )


def scope_subset(candidate, ceiling):
    """Typed collection containment; strings never act as ID iterables."""
    for kind, values in candidate.items():
        if kind not in ceiling:
            return False
        if values == OWNER_SCOPE:
            if not personal_owner(candidate) or not personal_owner(ceiling):
                return False
        elif values == "*":
            if kind != "memory" or ceiling[kind] not in ("*", OWNER_SCOPE):
                return False
        elif not isinstance(values, list) or any(
            not allows(ceiling, kind, identity) for identity in values
        ):
            return False
    return True


def grant_ids(types, ids):
    if not isinstance(ids, dict) or set(ids) != set(types):
        raise ValueError("Exact content IDs required")
    if set(types) - TYPES:
        raise ValueError("Unsupported content types")
    if any(value == OWNER_SCOPE for value in ids.values()):
        if not personal_owner(ids):
            raise ValueError("Complete personal owner scope required")
        return
    for kind, values in ids.items():
        if kind == "memory" and values == "*":
            continue
        if (
            not isinstance(values, list)
            or len(values) > 256
            or any(
                not isinstance(v, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", v)
                for v in values
            )
            or len(set(values)) != len(values)
        ):
            raise ValueError("Invalid content IDs")


def allows(ids, kind, identity):
    if (
        kind not in TYPES
        or not isinstance(identity, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", identity)
    ):
        return False
    values = ids.get(kind, [])
    return (
        personal_owner(ids)
        or kind == "memory"
        and values == "*"
        or isinstance(values, list)
        and identity in values
    )
