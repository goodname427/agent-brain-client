"""Compile this repo's ordinary Keychain helper; self-check never accesses secrets."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    _ENTRY_ROOT = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(_ENTRY_ROOT / "src"), str(_ENTRY_ROOT)]


from agent_brain_client.adapters.platforms.macos.helper_build import build


import argparse
import json

ROOT = Path(__file__).resolve().parents[2]


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=ROOT / ".local/credential-helper")
    p.add_argument(
        "--access",
        action="store_true",
        help="Build the separate address/key credential helper; preserve the legacy device helper.",
    )
    a = p.parse_args()
    print(json.dumps(build(a.output, access=a.access), sort_keys=True))
