"""domain / events: one owner for each contract and rule."""

import re
from agent_brain_client.domain.validation import encode
from agent_brain_client.domain.validation import query_time
from agent_brain_client.domain.validation import text


def event_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value
    ):
        raise ValueError("Bounded event identity required")


def event(value):
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "contract",
            "event_id",
            "project_id",
            "session_id",
            "occurred_at",
            "messages",
        }
        or value["contract"] != "agentbrain.event.v1"
    ):
        raise ValueError("Exact event required")
    for name in ("event_id", "project_id", "session_id"):
        event_identifier(value[name])
    stamp = value["occurred_at"]
    if not isinstance(stamp, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z", stamp
    ):
        raise ValueError("UTC event time required")
    query_time(stamp)
    messages = value["messages"]
    if not isinstance(messages, list) or not 1 <= len(messages) <= 100:
        raise ValueError("Bounded event messages required")
    for message in messages:
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message["role"] not in ("user", "assistant")
        ):
            raise ValueError("Event message required")
        text(message["content"], 32768)
        if len(message["content"].encode("utf-16-le")) // 2 > 8192:
            raise ValueError("Event message exceeds bound")
    if len(encode(value)) > 120000:
        raise ValueError("Event exceeds bound")
