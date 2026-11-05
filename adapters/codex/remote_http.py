"""Compatibility entry for the shared brain_http implementation."""
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from brain_http import *  # noqa: F403 - public compatibility exports for static consumers
import brain_http as _shared


def __getattr__(name: str) -> Any:
    return getattr(_shared, name)


if __name__ == '__main__':
    _shared.main()
else:
    sys.modules[__name__] = _shared
