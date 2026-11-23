"""Review explicit source/candidate metadata offline; no collection or upload.

Classifications are supplied evidence, not permission grants or NLP verdicts.
Daily review evaluates useful future context without demanding major/permanent value.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    _ENTRY_ROOT = Path(__file__).resolve().parents[4]
    sys.path[:0] = [str(_ENTRY_ROOT / "src"), str(_ENTRY_ROOT)]


import json

from agent_brain_client.domain.sources import review_memory_sources


def main():
    try:
        raw = sys.stdin.buffer.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("Bounded review metadata required")
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {
            "mode",
            "sources",
            "candidates",
        }:
            raise ValueError("Explicit review required")
        if value["mode"] != "daily":
            raise ValueError("Explicit daily review required")
        result = review_memory_sources(value["sources"], value["candidates"])
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, TypeError, KeyError):
        print('{"status":"source-review-invalid","data_transferred":false}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
