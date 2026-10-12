"""Review explicit source/candidate metadata offline; no collection or upload.

Classifications are supplied evidence, not permission grants or NLP verdicts.
Onboard preserves every authorized existing memory except sensitive/scope gaps;
daily evaluates useful future context without demanding major/permanent value.
"""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters/codex"))
from content_contract import review_memory_sources


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
        result = review_memory_sources(
            value["sources"], value["candidates"], value["mode"]
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, TypeError, KeyError):
        print('{"status":"source-review-invalid","data_transferred":false}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
