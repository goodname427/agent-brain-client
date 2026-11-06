"""MCP JSON-RPC errors, bounded messages and UTF-8 framing."""

import json

MAX_MESSAGE = 131072


class ProtocolError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def keys(value, allowed, required=()):
    if (
        not isinstance(value, dict)
        or set(value) - set(allowed)
        or set(required) - set(value)
    ):
        raise ProtocolError(-32602, "Invalid parameters; preserve existing data.")


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def write_response(output, response):
    # MCP is UTF-8 JSON, but redirected Python stdout may use GBK/cp1252.
    # Escape only the outer envelope: ASCII is valid UTF-8 on all supported
    # code pages, and JSON decoding restores every nested Unicode value.
    output.write(json.dumps(response, ensure_ascii=True) + "\n")
    output.flush()


class ContentParameterError(ProtocolError):
    def __init__(self, message):
        super().__init__(-32602, message)


class ContentToolUnavailable(ContentParameterError):
    pass
