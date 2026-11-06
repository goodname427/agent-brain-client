"""domain / validation: one owner for each contract and rule."""

from datetime import datetime, timezone
import json
import re


def encode(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()


def text(value, limit=65536):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode()) > limit
        or "\0" in value
    ):
        raise ValueError("Invalid text")


def strings(value, limit=32):
    if (
        not isinstance(value, list)
        or len(value) > limit
        or any(
            not isinstance(v, str)
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.+-]{0,63}", v)
            for v in value
        )
        or len(set(value)) != len(value)
    ):
        raise ValueError("Invalid dependencies")


def query_time(value):
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ValueError("ISO8601 time required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Explicit time zone required")
    return parsed.astimezone(timezone.utc)
