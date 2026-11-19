"""Read user-authored text from the rollout of the current Codex Stop only."""

import json
import os
from pathlib import Path
import re
import stat

from agent_brain_client.adapters.platforms.source_files import (
    native_source_stat,
    open_native_source,
)
from agent_brain_client.adapters.storage.metadata import unique
from agent_brain_client.domain import contract


# Bound the read independently of session length. Codex can also omit the path.
_TAIL_LIMIT = 8 * 1048576
_TEXT_LIMIT = 1048576
_FORM_TOOL = "request_user_input_async"
_FORM_TAG = "<send_user_message_question_reply>"
_FORM_END = "</send_user_message_question_reply>"


def form_reply(text):
    return text.startswith(_FORM_TAG)


def _path(native_home, payload):
    raw = payload.get("transcript_path")
    if not isinstance(raw, str):
        raise ValueError("Current Codex rollout required")
    path = Path(raw)
    root = Path(native_home) / "sessions"
    parts = path.relative_to(root).parts
    session = re.escape(payload["session_id"])
    if (
        not path.is_absolute()
        or len(parts) != 4
        or not re.fullmatch(r"[0-9]{4}/[0-9]{2}/[0-9]{2}", "/".join(parts[:3]))
        or not re.fullmatch(
            rf"rollout-[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T[0-9]{{2}}-[0-9]{{2}}-[0-9]{{2}}-{session}\.jsonl",
            parts[-1],
        )
    ):
        raise ValueError("Current Codex rollout required")
    return path


def _current_rows(path, session_id, turn_id, cwd):
    initial = native_source_stat(path)
    if not stat.S_ISREG(initial.st_mode):
        raise ValueError("Current Codex rollout must be a regular file")
    stream, info = open_native_source(path)
    with stream:
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ValueError("Current Codex rollout must be owned by the user")
        header = json.loads(stream.readline(_TEXT_LIMIT), object_pairs_hook=unique)
        if header["type"] != "session_meta" or header["payload"]["id"] != session_id:
            raise ValueError("Current Codex rollout session changed")
        start = max(0, info.st_size - _TAIL_LIMIT)
        stream.seek(start)
        lines = stream.read(info.st_size - start).split(b"\n")
        # A concurrent append can leave its last line incomplete.
        lines = lines[bool(start):-1]
        after = native_source_stat(path)
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino) or after.st_size < info.st_size:
            raise ValueError("Current Codex rollout changed")

    rows = []
    for line in reversed(lines):
        if not line:
            continue
        row = json.loads(line, object_pairs_hook=unique)
        item = row["payload"]
        if row["type"] == "event_msg" and item.get("type") == "task_started":
            if item.get("turn_id") == item.get("root_turn_id") == turn_id:
                break
            raise ValueError("Current Codex rollout turn changed")
        rows.append(row)
    else:
        raise ValueError("Current Codex turn is outside the rollout read bound")
    if not any(
        row["type"] == "turn_context"
        and row["payload"].get("turn_id") == row["payload"].get("root_turn_id") == turn_id
        and row["payload"].get("cwd") == cwd
        for row in rows
    ):
        raise ValueError("Current Codex rollout workspace changed")
    return reversed(rows)


def _user_text(item, metadata):
    text = []
    for part, kind, source in zip(
        item["content"], metadata["content_item_kinds"],
        metadata["content_item_metadata"], strict=True,
    ):
        if (
            kind == "user.text"
            and source.get("provenance") == {"type": "user"}
            and part.get("type") == "input_text"
        ):
            text.append(part["text"])
    return "\n".join(text)


def _form_answers(text, calls):
    if not text.startswith(_FORM_TAG + "\n") or not text.endswith("\n" + _FORM_END):
        raise ValueError("Unknown Codex form reply")
    entries = json.loads(text[len(_FORM_TAG) + 1:-len(_FORM_END) - 1], object_pairs_hook=unique)
    if not isinstance(entries, list) or not entries:
        raise ValueError("Unknown Codex form reply")
    answers = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"questionItemId", "question", "answer"}:
            raise ValueError("Unknown Codex form reply")
        name, call_id, index = json.loads(entry["questionItemId"])
        if name != _FORM_TOOL or call_id not in calls or type(index) is not int or index < 0:
            raise ValueError("Codex form reply has no current form")
        answer = entry["answer"]
        contract.text(answer, _TEXT_LIMIT)
        answers.append(answer)
    return "\n".join(answers)


def user_messages(native_home, payload):
    """Return (native ID, original text, memory text) in host order."""
    turn_id = payload["turn_id"]
    path = _path(native_home, payload)
    calls = set()
    messages = {}
    for row in _current_rows(path, payload["session_id"], turn_id, payload["cwd"]):
        item = row["payload"]
        if row["type"] != "response_item":
            continue
        metadata = item.get("internal_chat_message_metadata_passthrough")
        if not isinstance(metadata, dict) or metadata.get("turn_id") != turn_id:
            continue
        if item.get("type") == "function_call" and item.get("name") == _FORM_TOOL:
            calls.add(item["call_id"])
        if item.get("type") != "message" or item.get("role") != "user":
            continue
        original = _user_text(item, metadata)
        if not original.strip():
            continue
        contract.text(original, _TEXT_LIMIT)
        message_id = item["id"]
        contract.event_identifier(message_id)
        text = _form_answers(original, calls) if form_reply(original) else original
        if messages.setdefault(message_id, (original, text)) != (original, text):
            raise ValueError("Codex rollout message identity changed")
    return [(message_id, original, text) for message_id, (original, text) in messages.items()]
