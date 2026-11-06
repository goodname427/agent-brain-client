"""JSON-line Client request interface over the approved HTTP transport."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    root = Path(__file__).resolve().parents[4]
    sys.path[:0] = [str(root / "src"), str(root)]

import argparse
import json
from agent_brain_client.adapters.server.http import RemoteClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument(
        "--data-mode", choices=("synthetic", "personal"), default="synthetic"
    )
    args = parser.parse_args()
    client = RemoteClient(args.endpoint, args.token_file, args.data_mode)
    for line in sys.stdin:
        try:
            if len(line.encode()) > 131072:
                raise ValueError("Oversized request")
            result = client.request(json.loads(line))
        except Exception:
            result = {
                "error": {"code": "invalid_client_request", "retryable": False},
                "persistence": {"durable": False},
            }
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
