"""Build from this independent Client repository, never from an external Core."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    _ENTRY_ROOT = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(_ENTRY_ROOT / "src"), str(_ENTRY_ROOT)]


from agent_brain_client.adapters.packages.build import export, release


import argparse
import json

ROOT = Path(__file__).resolve().parents[2]
from agent_brain_client.adapters.packages.client import (
    CLIENT_FILES,
    CONTENT_CLIENT_FILES,
)
from agent_brain_client.adapters.packages.publication import require_publishable

FILES = {n: n for n in CLIENT_FILES}
PAIRING_FILES = {
    n: n
    for n in (
        "src/agent_brain_client/application/installation/pairing.py",
        "src/agent_brain_client/adapters/platforms/macos/native/DeviceComponent.swift",
    )
}
CONTENT_FILES = {
    n: n
    for n in CONTENT_CLIENT_FILES
    if n not in CLIENT_FILES and n not in PAIRING_FILES
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--version")
    parser.add_argument(
        "--mode",
        choices=[
            "synthetic",
            "personal",
            "personal-content",
            "personal-content-ssh",
            "personal-content-https",
        ],
        default="personal-content-https",
    )
    parser.add_argument(
        "--platform",
        choices=["darwin-arm64", "darwin-x86_64", "win32-amd64", "win32-arm64"],
    )
    parser.add_argument("--scan", action="store_true")
    a = parser.parse_args()
    if a.scan:
        payload = {
            p.relative_to(ROOT).as_posix(): p.read_bytes()
            for p in ROOT.rglob("*")
            if p.is_file()
            and not any(
                part in (".git", ".local", "__pycache__", ".ruff_cache", ".mypy_cache")
                for part in p.relative_to(ROOT).parts
            )
        }
        print(json.dumps(require_publishable(payload)))
        return
    if a.output is None:
        parser.error("--output is required for a build")
    print(
        json.dumps(
            release(ROOT, a.output, a.version, a.mode, a.platform)
            if a.version
            else export(
                ROOT,
                a.output,
                content_candidate=a.mode == "personal-content",
                ssh_content_candidate=a.mode == "personal-content-ssh",
                https_content_candidate=a.mode == "personal-content-https",
                target_platform=a.platform,
            )
        )
    )


if __name__ == "__main__":
    main()
