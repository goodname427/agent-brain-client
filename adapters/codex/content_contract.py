"""Compatibility import for the Harness-independent content protocol."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from brain_content import *  # noqa: F403 - public compatibility exports for static consumers
import brain_content as _shared
sys.modules[__name__] = _shared
