"""Versioned Server output for an automatic recall Hook."""

from dataclasses import dataclass
import hashlib
import re


FORMAT = "agentbrain.final-context.v1"
CAPABILITY = "final-context-v1"
TIME_ZONE_CAPABILITY = "recall-time-zone-v1"
# Model-request bytes observed through the Mac CLI entry points. The CodeBuddy
# measurement exceeds the protocol ceiling. Keep the host-specific caps until
# the same probes have run on Windows and in the IDE.
HOST_BYTES = {"codex": 10000, "codebuddy": 24576}
LAYERS = ("L1", "L2", "L3")
MAX_SOURCES = 128


@dataclass(frozen=True)
class FinalContext:
    body: str
    references: tuple[dict[str, str], ...]


def check_response(response, budget, verify_source):
    persistence = response.get("persistence")
    if (
        "error" in response
        or not isinstance(persistence, dict)
        or set(persistence) != {"state", "durable"}
        or persistence["state"] != "read-only"
        or persistence["durable"] is not False
    ):
        raise ValueError("Authenticated read-only final recall required")
    result = response.get("result")
    if (
        not isinstance(result, dict)
        or set(result) != {"schema", "final_context"}
        or type(result["schema"]) is not int
        or result["schema"] != 2
    ):
        raise ValueError("Versioned final recall required")
    final = result["final_context"]
    if (
        not isinstance(final, dict)
        or set(final) != {"format", "body", "body_sha256", "sources"}
        or final["format"] != FORMAT
        or not isinstance(final["body"], str)
        or not isinstance(final["sources"], list)
        or len(final["sources"]) > MAX_SOURCES
        or bool(final["body"]) != bool(final["sources"])
    ):
        raise ValueError("Exact final recall body and sources required")
    raw = final["body"].encode()
    if len(raw) > budget or hashlib.sha256(raw).hexdigest() != final["body_sha256"]:
        raise ValueError("Final recall body changed or exceeded host capacity")
    references = []
    for source in final["sources"]:
        if (
            not isinstance(source, dict)
            or set(source) != {
                "layer", "project_id", "sharing", "source_device_id", "source_context",
                "provenance", "record_sha256", "content_sha256",
            }
            or source["layer"] not in LAYERS
            or not all(
                isinstance(source[key], str) and re.fullmatch("[a-f0-9]{64}", source[key])
                for key in ("record_sha256", "content_sha256")
            )
        ):
            raise ValueError("Exact final recall source required")
        verify_source(source)
        references.append({key: source[key] for key in ("layer", "record_sha256", "content_sha256")})
    return FinalContext(final["body"], tuple(references))
