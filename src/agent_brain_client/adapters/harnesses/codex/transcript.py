"""Read bounded, source-verified increments from the current Codex rollout."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat

from agent_brain_client.adapters.platforms.source_files import (
    native_source_stat,
    open_native_source,
)
from agent_brain_client.adapters.storage.metadata import digest, unique
from agent_brain_client.domain import contract, privacy
from agent_brain_client.domain.scopes import restricted


# Bound the read independently of session length. Codex can also omit the path.
_TAIL_LIMIT = 8 * 1048576
_TEXT_LIMIT = 1048576
_FORM_TOOL = "request_user_input_async"
_FORM_TAG = "<send_user_message_question_reply>"
_FORM_END = "</send_user_message_question_reply>"


def form_reply(text):
    return text.startswith(_FORM_TAG)


def private(text):
    return privacy.private_content(text) or restricted(text)


@dataclass
class Snapshot:
    binding: dict
    end: int
    size: int
    rows: list
    incomplete: bool
    gap_at: int | None


@dataclass
class Observations:
    messages: list
    forms: dict
    private: bool = False
    unknown_form: bool = False
    unmatched_form: bool = False
    incomplete: bool = False


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


def _read_rows(stream, start, stop, *, newest_turn=False):
    stream.seek(max(0, start - 1))
    aligned = not start or stream.read(1) == b"\n"
    pieces = stream.read(stop - start).split(b"\n")
    end = stop - len(pieces[-1])
    lines = pieces[:-1]
    position = end if newest_turn else start
    rows = []
    damaged_at = None
    for raw in reversed(lines) if newest_turn else lines:
        width = len(raw) + 1
        offset = position - width if newest_turn else position
        position += -width if newest_turn else width
        if not raw or (offset == start and not aligned):
            continue
        try:
            row = json.loads(raw, object_pairs_hook=unique)
            if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
                raise ValueError("Unknown Codex rollout row")
        except (ValueError, RecursionError):
            damaged_at = offset
            continue
        boundary = _event(row, "task_started")
        checksum = digest(raw) if boundary or row.get("type") == "turn_context" else None
        rows.append((offset, checksum, row))
        if newest_turn and boundary:
            break
    if newest_turn:
        rows.reverse()
    return rows, end, damaged_at


def _event(row, name):
    return row.get("type") == "event_msg" and row["payload"].get("type") == name


def _pin(stream, pin, maximum):
    offset, checksum = pin
    if type(offset) is not int or not 0 <= offset < maximum:
        raise ValueError("Current Codex rollout bookmark changed")
    stream.seek(offset)
    line = stream.readline(_TEXT_LIMIT)
    if not line.endswith(b"\n") or digest(line[:-1]) != checksum:
        raise ValueError("Current Codex rollout bookmark changed")


def _pin_scope(rows, turn_id, cwd):
    if not rows or not _event(rows[0][2], "task_started"):
        raise ValueError("Current Codex turn is outside the rollout read bound")
    start, checksum, boundary = rows[0]
    item = boundary["payload"]
    if item.get("turn_id") != turn_id or item.get("root_turn_id") != turn_id:
        raise ValueError("Current Codex rollout turn changed")
    current = rows[1:]
    for offset, context_sha, row in current:
        if row.get("type") == "turn_context" and row["payload"].get("turn_id") == turn_id:
            if row["payload"].get("root_turn_id") != turn_id or row["payload"].get("cwd") != cwd:
                raise ValueError("Current Codex rollout workspace changed")
            return [[start, checksum], [offset, context_sha]], current
    raise ValueError("Current Codex rollout workspace unavailable")


def snapshot(native_home, payload, *, binding=None, cursor=None):
    path = _path(native_home, payload)
    session_id, turn_id = payload["session_id"], payload["turn_id"]
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
        identity = [info.st_dev, info.st_ino, digest(os.fsencode(path))]
        size = info.st_size
        gap_at = None
        if binding is None:
            rows, end, damaged_at = _read_rows(
                stream, max(0, size - _TAIL_LIMIT), size, newest_turn=True,
            )
            pins, rows = _pin_scope(rows, turn_id, payload["cwd"])
            binding = {"identity": identity, "pins": pins}
            damaged = damaged_at is not None and damaged_at > pins[0][0]
        else:
            if (identity != binding["identity"] or type(cursor) is not int
                    or not 0 < cursor <= size or len(binding["pins"]) != 2):
                raise ValueError("Current Codex rollout cursor changed")
            for pin in binding["pins"]:
                _pin(stream, pin, cursor)
            # A long idle interval can put the turn boundary outside the tail.
            # Keep the first unseen rows and the recent suffix within one budget.
            if size - cursor > _TAIL_LIMIT:
                half = _TAIL_LIMIT // 2
                early, gap_at, _ = _read_rows(stream, cursor, cursor + half)
                recent, end, _ = _read_rows(stream, size - (_TAIL_LIMIT - half), size)
                rows, damaged = early + recent, True
            else:
                rows, end, damaged_at = _read_rows(stream, cursor, size)
                damaged = damaged_at is not None
        _check_increment(rows, turn_id, payload["cwd"])
        after = native_source_stat(path)
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino) or after.st_size < size:
            raise ValueError("Current Codex rollout changed")
    return Snapshot(binding, end, size, [(offset, row) for offset, _, row in rows],
                    damaged or end != size, gap_at)


def _check_increment(rows, turn_id, cwd):
    for _, _, row in rows:
        item = row["payload"]
        if _event(row, "task_started") and (item.get("turn_id"), item.get("root_turn_id")) != (turn_id, turn_id):
            raise ValueError("Current Codex rollout turn changed")
        if row.get("type") == "turn_context" and (
            item.get("turn_id"), item.get("root_turn_id"), item.get("cwd")
        ) != (turn_id, turn_id, cwd):
            raise ValueError("Current Codex rollout workspace changed")


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


def form_answers(text):
    if not text.startswith(_FORM_TAG + "\n") or not text.endswith("\n" + _FORM_END):
        raise ValueError("Unknown Codex form reply")
    entries = json.loads(text[len(_FORM_TAG) + 1:-len(_FORM_END) - 1], object_pairs_hook=unique)
    if not isinstance(entries, list) or not entries:
        raise ValueError("Unknown Codex form reply")
    answers = []
    calls = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"questionItemId", "question", "answer"}:
            raise ValueError("Unknown Codex form reply")
        name, call_id, index = json.loads(entry["questionItemId"])
        if name != _FORM_TOOL or type(index) is not int or index < 0:
            raise ValueError("Unknown Codex form identity")
        contract.event_identifier(call_id)
        calls.add(call_id)
        answer = entry["answer"]
        contract.text(answer, _TEXT_LIMIT)
        answers.append(answer)
    return "\n".join(answers), calls


def user_messages(rows, turn_id, known_forms):
    """Derive user messages, form privacy and form call evidence in host order."""
    messages = {}
    conflicts = set()
    observed = Observations([], dict(known_forms))
    for offset, row in rows:
        item = row["payload"]
        if row["type"] != "response_item":
            continue
        metadata = item.get("internal_chat_message_metadata_passthrough")
        if not isinstance(metadata, dict) or metadata.get("turn_id") != turn_id:
            continue
        if item.get("type") == "function_call" and item.get("name") == _FORM_TOOL:
            call = item.get("call_id")
            try:
                contract.event_identifier(call)
            except ValueError:
                observed.incomplete = True
                continue
            if call in observed.forms or len(observed.forms) < 256:
                observed.forms.setdefault(call, True)
            else:
                observed.unknown_form = True
        if item.get("type") != "message" or item.get("role") != "user":
            continue
        try:
            original = _user_text(item, metadata)
            if not original.strip():
                continue
            contract.text(original, _TEXT_LIMIT)
        except (ValueError, TypeError, KeyError, AttributeError):
            observed.incomplete = True
            continue
        content = original
        is_form = form_reply(original)
        if is_form:
            try:
                content, refs = form_answers(original)
            except (ValueError, TypeError, KeyError, RecursionError):
                observed.unknown_form = True
                continue
            if private(content) or private(original):
                observed.private = True
            if not refs <= observed.forms.keys():
                observed.unmatched_form = True
                continue
            for call in refs:
                observed.forms[call] = False
        elif private(original):
            observed.private = True
        message_id = item.get("id")
        try:
            contract.event_identifier(message_id)
        except ValueError:
            observed.incomplete = True
            continue
        message = (offset, message_id, original, content, is_form)
        if messages.setdefault(message_id, message)[2:] != message[2:]:
            conflicts.add(message_id)
            observed.incomplete = True
    observed.messages = [message for message in messages.values() if message[1] not in conflicts]
    return observed
