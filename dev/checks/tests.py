"""Run selected unittest modules or full discovery, optionally on two workers."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    _ENTRY_ROOT = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(_ENTRY_ROOT / "src"), str(_ENTRY_ROOT)]


import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import re
import subprocess
import tempfile
import time

from dev.checks.timing import summaries


def run_group(modules):
    # Each process gets its own homes, just as separate CI jobs would.
    with tempfile.TemporaryDirectory(prefix="client-test-worker-") as temp:
        home, codex = Path(temp) / "home", Path(temp) / "codex"
        home.mkdir()
        codex.mkdir()
        env = dict(
            os.environ, HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(codex)
        )
        start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).with_name("timing.py")), *modules],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return result.returncode, result.stdout, round(time.monotonic() - start, 3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=1)
    parser.add_argument(
        "--modules", nargs="+", help="Explicit native-platform test selectors"
    )
    args = parser.parse_args()
    tests = args.modules or json.loads(os.environ.get("CI_TESTS", "[]"))
    if not isinstance(tests, list) or any(
        not isinstance(t, str)
        or not re.fullmatch(r"test_[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*", t)
        for t in tests
    ):
        raise ValueError("Expected a JSON list of test module names")
    if args.workers == 1:
        raise SystemExit(
            subprocess.call(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "unittest",
                    *(tests or ["discover", "-s", "tests"]),
                ]
            )
        )
    # The current suite is flat. Preserve discovery if packages are introduced.
    if not tests and any(Path("tests").glob("*/__init__.py")):
        raise SystemExit(
            subprocess.call(
                [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"]
            )
        )
    modules = tests or sorted(path.stem for path in Path("tests").glob("test*.py"))
    if not modules:
        raise ValueError("No test modules found")
    # Feed one module at a time to each available worker. Adding a file must
    # not reshuffle two fixed batches and leave one worker idle for minutes.
    groups = [[module] for module in modules]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(run_group, groups))
    for module, (code, log, seconds) in zip(modules, results):
        print(f"{module}: {seconds}s, exit {code}", flush=True)
        for line in summaries(log):
            print(line, flush=True)
        print(log, end="", flush=True)
    raise SystemExit(1 if any(code for code, _, _ in results) else 0)


if __name__ == "__main__":
    main()
