"""Read bounded, source-verified increments from the current Codex rollout."""

import codecs
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
_LARGE_ROW_LIMIT = 64 * 1048576
_ROW_CHUNK = 65536
_STRING_SPECIAL = re.compile(rb'["\\\x00-\x1f]')
_JSON_ESCAPE = re.compile(rb'\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})')
_BULK_FIELD = re.compile(rb'"(?:output|image_url)"[ \t\r]*:[ \t\r]*$')
_FORK_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
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
    more: bool
    observed_cursor: int | None = None
    review: dict | None = None


@dataclass
class Observations:
    messages: list
    forms: dict
    private: bool = False
    unknown_form: bool = False
    unmatched_form: bool = False
    incomplete: bool = False


class RolloutScopeUnavailable(ValueError):
    """The current turn's scope is not visible in the available rollout bytes."""


class OversizedRowUnverified(ValueError):
    """A bounded scan cannot safely classify an oversized JSONL record."""


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
            rf"rollout-[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T[0-9]{{2}}-[0-9]{{2}}-[0-9]{{2}}-"
            rf"{session}(?:_{_FORK_UUID})?\.jsonl",
            parts[-1],
        )
    ):
        raise ValueError("Current Codex rollout required")
    return path


def _identity(path, info):
    return [info.st_dev, info.st_ino, digest(os.fsencode(path))]


def observation_position(native_home, payload):
    """Record an append boundary even when rollout contents are not yet readable."""
    path = _path(native_home, payload)
    info = native_source_stat(path)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or hasattr(os, "getuid") and info.st_uid != os.getuid()):
        raise ValueError("Current Codex rollout must be an owned regular file")
    return {"identity": _identity(path, info), "cursor": info.st_size}


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


def _count_elisions(value, marker):
    if value is marker:
        return 1
    if isinstance(value, dict):
        return sum(_count_elisions(item, marker) for item in value.values())
    if isinstance(value, list):
        return sum(_count_elisions(item, marker) for item in value)
    return 0


def _verified_projection(projected, dropped):
    marker = object()
    decoded = 0

    def constant(name):
        nonlocal decoded
        if name != "NaN":
            raise ValueError("Invalid JSON constant")
        decoded += 1
        return marker

    row = json.loads(projected, object_pairs_hook=unique, parse_constant=constant)
    if not isinstance(row, dict) or not isinstance(row.get("payload"), dict) or decoded != dropped:
        raise ValueError("Invalid JSONL projection")
    item = row["payload"]
    if dropped:
        allowed = 0
        if row.get("type") == "response_item":
            if item.get("type") in ("function_call_output", "custom_tool_call_output"):
                allowed = _count_elisions(item.get("output"), marker)
            elif item.get("type") == "message" and isinstance(item.get("content"), list):
                for part in item["content"]:
                    if (isinstance(part, dict) and part.get("type") == "input_image"
                            and part.get("image_url") is marker):
                        allowed += 1
                        part["image_url"] = ""
        if allowed != dropped:
            raise OversizedRowUnverified("oversized-rollout-unverified-fields")

    if row.get("type") == "response_item" and item.get("type") == "message" and item.get("role") == "user":
        try:
            metadata = item["internal_chat_message_metadata_passthrough"]
            if not isinstance(metadata["turn_id"], str):
                raise ValueError("Missing user message scope")
            original = _user_text(item, metadata)
            if original.strip():
                contract.text(original, _TEXT_LIMIT)
        except (ValueError, TypeError, KeyError, AttributeError):
            # An attachment may be elided, but its user's text still has to
            # fit the normal privacy check.
            raise OversizedRowUnverified("oversized-rollout-unverified-fields") from None
    return row


def _read_oversized_row(stream, offset, size):
    """Validate one large row without materializing its large JSON strings."""
    stream.seek(offset - 1)
    if stream.read(1) != b"\n":
        raise OversizedRowUnverified("oversized-rollout-unaligned")
    end = offset
    scan_end = min(size, offset + _LARGE_ROW_LIMIT)
    utf8 = codecs.getincrementaldecoder("utf-8")("strict")
    projected = bytearray()
    token = None
    token_limit = _TAIL_LIMIT
    inside = False
    carry = b""
    dropped = 0

    def append(data):
        if len(projected) + len(data) > _TAIL_LIMIT:
            raise OversizedRowUnverified("oversized-rollout-projection-limit")
        projected.extend(data)

    def retain(data):
        nonlocal token
        if token is not None:
            if len(token) + len(data) <= token_limit:
                token.extend(data)
            else:
                token = None

    try:
        while end < scan_end:
            chunk = stream.readline(min(_ROW_CHUNK, scan_end - end))
            if not chunk:
                break
            end += len(chunk)
            complete = chunk.endswith(b"\n")
            data = chunk[:-1] if complete else chunk
            utf8.decode(data, final=complete)
            data = carry + data
            carry = b""
            cursor = 0
            while cursor < len(data):
                if not inside:
                    quote = data.find(b'"', cursor)
                    if quote < 0:
                        append(data[cursor:])
                        break
                    append(data[cursor:quote])
                    token_limit = min(_TAIL_LIMIT, 6 * _TEXT_LIMIT + 2)
                    if _BULK_FIELD.search(projected[-128:]):
                        token_limit = min(token_limit, _ROW_CHUNK)
                    token = bytearray(b'"')
                    inside = True
                    cursor = quote + 1
                    continue
                special = _STRING_SPECIAL.search(data, cursor)
                if special is None:
                    retain(data[cursor:])
                    break
                position = special.start()
                retain(data[cursor:position])
                char = data[position:position + 1]
                cursor = position + 1
                if char == b'"':
                    retain(char)
                    if token is None:
                        dropped += 1
                        append(b"NaN")
                    else:
                        append(token)
                    inside = False
                elif char == b"\\":
                    width = 6 if data[cursor:cursor + 1] == b"u" else 2
                    escape = data[position:position + width]
                    if len(escape) < width:
                        carry = escape
                        break
                    if not _JSON_ESCAPE.fullmatch(escape):
                        raise ValueError("Invalid JSON escape")
                    retain(escape)
                    cursor = position + width
                else:
                    raise ValueError("Unescaped JSON control byte")
            if complete:
                if inside or carry:
                    raise ValueError("Unfinished JSON string")
                return _verified_projection(projected, dropped), end
    except OversizedRowUnverified:
        raise
    except (ValueError, RecursionError):
        raise OversizedRowUnverified("oversized-rollout-json-invalid") from None
    if end == offset + _LARGE_ROW_LIMIT:
        raise OversizedRowUnverified("oversized-rollout-scan-limit")
    return None, offset  # Still appending; the cursor stays before the record.


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
        raise RolloutScopeUnavailable("Current Codex turn is outside the rollout read bound")
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
    raise RolloutScopeUnavailable("Current Codex rollout workspace unavailable")


def snapshot(native_home, payload, *, binding=None, cursor=None, until=None, allow_next=False,
             observed_position=None):
    path = _path(native_home, payload)
    session_id, turn_id = payload["session_id"], payload["turn_id"]
    initial = native_source_stat(path)
    if not stat.S_ISREG(initial.st_mode):
        raise ValueError("Current Codex rollout must be a regular file")
    stream, info = open_native_source(path)
    with stream:
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ValueError("Current Codex rollout must be owned by the user")
        header_line = stream.readline(_TEXT_LIMIT)
        if not header_line.endswith(b"\n"):
            raise RolloutScopeUnavailable("Current Codex rollout header unavailable")
        header = json.loads(header_line, object_pairs_hook=unique)
        if header["type"] != "session_meta" or header["payload"]["id"] != session_id:
            raise ValueError("Current Codex rollout session changed")
        identity = _identity(path, info)
        size = info.st_size if until is None else until
        if type(size) is not int or not 0 < size <= info.st_size:
            raise ValueError("Current Codex rollout shrank")
        more = False
        review = None
        observed_cursor = None
        if binding is None:
            limit = size
            if observed_position is not None and observed_position["identity"] == identity:
                position = observed_position["cursor"]
                if type(position) is int and 0 <= position <= size:
                    # Look near the original observation for the turn pins.
                    # They may have fallen outside the latest tail by now.
                    observed_cursor = position
                    limit = min(size, position + _TAIL_LIMIT // 2)
            rows, end, damaged_at = _read_rows(
                stream, max(0, limit - _TAIL_LIMIT), limit, newest_turn=True,
            )
            pins, rows = _pin_scope(rows, turn_id, payload["cwd"])
            binding = {"identity": identity, "pins": pins}
            damaged = damaged_at is not None and damaged_at > pins[0][0]
            if observed_cursor is not None:
                observed_cursor = max(observed_cursor, pins[0][0])
                end = max(end, observed_cursor)
                more = limit < size
        else:
            if (identity != binding["identity"] or type(cursor) is not int
                    or not 0 < cursor <= size or len(binding["pins"]) != 2):
                raise ValueError("Current Codex rollout cursor changed")
            for pin in binding["pins"]:
                _pin(stream, pin, info.st_size)
            limit = min(cursor + _TAIL_LIMIT, size)
            rows, end, damaged_at = _read_rows(stream, cursor, limit)
            if end == cursor and limit < size:
                try:
                    row, end = _read_oversized_row(stream, cursor, size)
                    if row is not None:
                        # Scope pins must come from whole, bounded records.
                        if _event(row, "task_started") or row.get("type") == "turn_context":
                            raise OversizedRowUnverified("oversized-rollout-unverified-fields")
                        rows = [(cursor, None, row)]
                except OversizedRowUnverified as error:
                    review = {"offset": cursor, "reason": str(error)}
            more = end < size and review is None
            damaged = damaged_at is not None
        rows, boundary = _check_increment(rows, turn_id, payload["cwd"], allow_next)
        if boundary is not None:
            end = size = boundary
            more = False
        after = native_source_stat(path)
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino) or after.st_size < size:
            raise ValueError("Current Codex rollout changed")
    return Snapshot(binding, end, size, [(offset, row) for offset, _, row in rows],
                    damaged or (not more and end != size), more, observed_cursor, review)


def _check_increment(rows, turn_id, cwd, allow_next):
    for index, (offset, _, row) in enumerate(rows):
        item = row["payload"]
        if _event(row, "task_started") and (item.get("turn_id"), item.get("root_turn_id")) != (turn_id, turn_id):
            if allow_next and isinstance(item.get("turn_id"), str) and item["turn_id"] == item.get("root_turn_id"):
                return rows[:index], offset
            raise ValueError("Current Codex rollout turn changed")
        if row.get("type") == "turn_context" and (
            item.get("turn_id"), item.get("root_turn_id"), item.get("cwd")
        ) != (turn_id, turn_id, cwd):
            raise ValueError("Current Codex rollout workspace changed")
    return rows, None


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
