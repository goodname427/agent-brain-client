"""Local equivalents of Actions checks. Native receipts require the actual OS and Python.

Logs and receipts are private operator evidence, never GitHub success statuses.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    _ENTRY_ROOT = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(_ENTRY_ROOT / "src"), str(_ENTRY_ROOT)]


import argparse
import hashlib
from importlib.metadata import version
import json
import os
import platform
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://github.com/goodname427/agent-brain-client"
COMMANDS = {
    "release": [
        ["python", "-B", "dev/checks/docs.py"],
        ["python", "-B", "dev/checks/source.py"],
        [
            "python", "-B", "-m", "unittest",
            "test_install_contracts.PlatformMainContracts.test_shared_release_validation_preserves_permission_and_rollback_constraints",
            "test_install_contracts.InstallationArchitectureContracts",
            "test_install_contracts.Bootstrap.test_bad_digest_and_download_failure_never_execute_or_activate",
            "test_access_install.AccessTransport",
            "test_access_install.AccessTransaction",
            "test_release_pipeline.PlatformRelease.test_both_platforms_publish_before_atomic_channel_promotion",
            "test_release_pipeline.ReleasePipeline.test_public_download_corruption_is_rejected_without_auth_header",
        ],
    ],
    "documentation": [
        ["python", "-B", "dev/checks/docs.py"],
        ["python", "-B", "-m", "unittest", "test_check_docs", "test_ci_scope"],
        ["git", "diff", "--check"],
    ],
    "source": [
        [
            "python",
            "-B",
            "-m",
            "unittest",
            "test_release_pipeline",
            "test_client_checks",
            "test_architecture",
            "test_application_workflows",
            "test_collection_control",
            "test_ledger_ownership.OwnerGates.test_source_removal_inventory_is_read_only_without_transport_identity",
            "test_ledger_ownership.OwnerGates.test_mapping_gate_preserves_read_only_settlement_semantics",
        ],
        ["python", "-B", "dev/build/package.py", "--scan"],
        ["python", "-B", "dev/checks/source.py"],
        ["git", "diff", "--check"],
        [
            "python",
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--select",
            "E4,E7,E9,F",
            "--ignore",
            "E402",
            "src",
            "dev",
            "bootstrap",
            "--exclude",
            "src/agent_brain_client/adapters/platforms/windows/native.py",
        ],
        [
            "python",
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--select",
            "F821,F822,F823,F841",
            "src/agent_brain_client/adapters/platforms/windows/native.py",
        ],
        [
            "python",
            "-m",
            "mypy",
            "--check-untyped-defs",
            "--follow-imports",
            "skip",
            "--ignore-missing-imports",
            "src/agent_brain_client/domain",
            "src/agent_brain_client/application",
        ],
    ],
    "macos": [
        ["python", "-B", "-m", "unittest", "discover", "-s", "tests"],
        ["sh", "-n", "bootstrap/macos/install.sh"],
        ["sh", "-n", "bin/memory.sh"],
        ["git", "diff", "--check"],
        [
            "python",
            "-B",
            "dev/build/credentials.py",
            "--output",
            "{temp}/credential-helper",
        ],
        [
            "python",
            "-B",
            "dev/build/credentials.py",
            "--access",
            "--output",
            "{temp}/access-helper",
        ],
    ],
    "windows": [
        [
            "python",
            "-B",
            "-m",
            "unittest",
            "test_architecture",
            "test_application_workflows",
            "test_collection_control",
            "test_ledger_ownership.OwnerGates.test_source_removal_inventory_is_read_only_without_transport_identity",
            "test_ledger_ownership.OwnerGates.test_mapping_gate_preserves_read_only_settlement_semantics",
            "test_harness_core",
            "test_harness_parity",
            "test_host_entrypoints",
            "test_runtime_boundaries",
            "test_rule_fallback",
        ],
        [
            "python",
            "-B",
            "-m",
            "unittest",
            "test_startup_concurrency",
            "test_global_memory_update",
            "test_global_memory_activation",
        ],
        [
            "python",
            "-B",
            "-m",
            "unittest",
            "test_fresh_installation",
            "test_fresh_installation_controls",
            "test_fresh_project_stops",
            "test_install_contracts.Contracts",
            "test_install_contracts.PlatformMainContracts",
            "test_install_contracts.InstallationArchitectureContracts",
            "test_windows_native",
            "test_windows_private_metadata",
            "test_host_config.WindowsBoundary",
            "test_install_plan",
            "test_source_paths",
            "test_native_source_files",
            "test_non_git_origins",
            "test_access_install",
            "test_bootstrap_main",
            "test_platform_checkpoint",
        ],
        [
            "python",
            "-B",
            "-m",
            "unittest",
            "test_mcp_wire_encoding",
            "test_mcp_queue_deadlines",
            "test_content_read_requests.HTTPRequests",
        ],
        ["python", "-B", "dev/build/package.py", "--scan"],
        ["python", "-B", "dev/checks/source.py"],
        [
            "python",
            "-B",
            "-m",
            "unittest",
            "test_content_write_failures",
            "test_content_activation",
            "test_content_incremental_refresh",
        ],
        [
            "python",
            "-B",
            "-m",
            "unittest",
            "test_layered_recall",
            "test_runtime_reload",
            "test_codebuddy",
            "test_codebuddy_gateway",
            "test_shared_lifecycle",
            "test_background_delivery",
            "test_capture_crash_recovery",
            "test_codebuddy_output_ack",
            "test_observed_migration",
            "test_project_lifecycle",
        ],
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference = 'Stop'; foreach ($file in @('bootstrap/windows/install.ps1', 'bin/memory.ps1', 'bootstrap/windows/python.ps1')) { $errors = $null; [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $pwd $file), [ref]$null, [ref]$errors) | Out-Null; if ($errors.Count) { throw ($errors | Out-String) } }",
        ],
    ],
}


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def git(*args, root=ROOT):
    return subprocess.check_output(["git", *args], cwd=root).decode().strip()


def identity(root=ROOT):
    if git("status", "--porcelain", "--untracked-files=all", root=root):
        raise ValueError("Recording checks requires a clean committed checkout")
    if git("remote", "get-url", "origin", root=root) not in (ORIGIN, ORIGIN + ".git"):
        raise ValueError("Official credential-free Client origin required")
    from dev.release import client as release_client

    source = git("rev-parse", "HEAD", root=root)
    with release_client.source_root(root):
        release_client.source_payload(source)
    return {"source": source, "source_tree": git("rev-parse", "HEAD^{tree}", root=root)}


def check_host(scope):
    python = sys.version_info[:2]
    if scope == "release":
        if sys.platform not in ("darwin", "win32") or python < (3, 11):
            raise ValueError("Native macOS or Windows with Python 3.11+ required")
        return
    if scope == "windows":
        if sys.platform != "win32" or python not in ((3, 11), (3, 13)):
            raise ValueError("Native Windows with Python 3.11 or 3.13 required")
    elif python != (3, 12):
        raise ValueError("Python 3.12 required to match Client checks")
    if scope == "macos" and sys.platform != "darwin":
        raise ValueError("Native macOS required for Swift checks")
    if scope == "source":
        for package, expected in (("ruff", "0.14.0"), ("mypy", "1.18.2")):
            if version(package) != expected:
                raise ValueError(
                    "Pinned check tool required: " + package + "==" + expected
                )


def run_checks(scope, output, record=False, *, ci=False, root=ROOT):
    started_checks = time.monotonic() if scope == "release" else None
    if scope == "release" and not record:
        raise ValueError("Release preflight requires source-bound results")
    if ci and record:
        raise ValueError(
            "Focused CI runs cannot produce complete local release evidence"
        )
    check_host(scope)
    before = identity(root) if record else None
    output = Path(output).resolve()
    # Never overwrite evidence from a previous successful or failed run.
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    results = []
    with tempfile.TemporaryDirectory(prefix="client-checks-") as tmp:
        temp = Path(tmp)
        for name in ("home", "codex"):
            (temp / name).mkdir()
        env = dict(
            os.environ,
            HOME=str(temp / "home"),
            USERPROFILE=str(temp / "home"),
            CODEX_HOME=str(temp / "codex"),
            PYTHONDONTWRITEBYTECODE="1",
            MYPY_CACHE_DIR=str(temp / "mypy"),
            PYTHONPATH=os.pathsep.join(
                str(root / name)
                for name in (
                    ".",
                    "src",
                    "tests",
                )
            ),
        )
        # No check subprocess receives a publication credential or user config.
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "PYTHONHOME", "PYTHONSTARTUP"):
            env.pop(name, None)
        diagnose = ci and scope == "windows" and sys.version_info[:2] == (3, 11)
        if diagnose:
            env["AB_WINDOWS_311_DIAGNOSTIC"] = "1"
        commands = COMMANDS[scope]
        if ci and scope == "windows":
            # Same selectors on both Python versions; independent process homes
            # permit two native workers without sharing synthetic device state.
            selectors = list(
                dict.fromkeys(
                    test
                    for command in commands
                    if command[:4] == ["python", "-B", "-m", "unittest"]
                    for test in command[4:]
                )
            )
            commands = [
                [
                    "python",
                    "-B",
                    "dev/checks/tests.py",
                    "--workers",
                    "2",
                    "--modules",
                    *selectors,
                ],
                *(
                    command
                    for command in commands
                    if command[:4] != ["python", "-B", "-m", "unittest"]
                ),
            ]
        if ci and scope == "macos":
            commands = [
                ["python", "-B", "dev/checks/tests.py", "--workers", "2"],
                *commands[1:],
            ]
            if json.loads(env.get("CI_TESTS", "[]")):
                commands = [
                    command
                    for command in commands
                    if "dev/build/credentials.py" not in command
                ]
        if diagnose:
            commands = [
                ["python", "-B", "-m", "unittest", "-v", "test_codebuddy_output_ack"],
                *commands,
            ]
        for index, command in enumerate(commands):
            actual = [
                sys.executable
                if item == "python" and position == 0
                else item.replace("{temp}", str(temp))
                for position, item in enumerate(command)
            ]
            log_path = output / f"{index}.log"
            print(
                f"{scope}: {index + 1}/{len(commands)} " + " ".join(command[:6]),
                flush=True,
            )
            with log_path.open("xb") as log:
                started = time.monotonic()
                if diagnose and "dev/checks/tests.py" in command:
                    with subprocess.Popen(actual, cwd=root, env=env, stdout=log,
                                          stderr=subprocess.STDOUT) as process:
                        displayed = set()
                        deadline = time.monotonic() + 1800
                        while process.poll() is None:
                            for line in log_path.read_text(errors="replace").splitlines():
                                if line.startswith("NATIVE_TEST_") and line not in displayed:
                                    displayed.add(line)
                                    print("::notice title=Windows Python 3.11 module::" + line, flush=True)
                            if time.monotonic() >= deadline:
                                process.kill()
                                raise TimeoutError("Windows Python 3.11 full check exceeded 1800 seconds")
                            try:
                                process.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                pass
                        result = subprocess.CompletedProcess(actual, process.returncode)
                else:
                    result = subprocess.run(
                        actual, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                        timeout=None if scope == "release" else
                        3600 if scope == "macos" and record and index == 0 else 1800,
                    )
                if diagnose and index == 0:
                    print(f"::notice title=Windows Python 3.11 Hook regressions::exit {result.returncode}",
                          flush=True)
            seconds = round(time.monotonic() - started, 3)
            print(
                f"{scope}: command {index + 1} took {seconds}s, exit {result.returncode}",
                flush=True,
            )
            if "dev/checks/tests.py" in command:
                # Expose phase totals and the three slowest cases per selector;
                # complete per-case output stays in the worker log.
                for line in log_path.read_text(errors="replace").splitlines():
                    if (line.startswith("test_") and "s, exit " in line) or line.startswith(
                        ("TEST_PHASE_TOTALS ", "TEST_SLOW_CASE ")
                    ):
                        print(line, flush=True)
            results.append(
                {
                    "command": command,
                    "exit_code": result.returncode,
                    "seconds": seconds,
                    "log": log_path.name,
                    "sha256": sha(log_path.read_bytes()),
                }
            )
            if result.returncode:
                print(log_path.read_text(errors="replace"), file=sys.stderr)
                raise ValueError(f"{scope} check failed; inspect {log_path.name}")
    if record:
        if identity(root) != before:
            raise ValueError("Source changed while checks ran")
        sys.path.insert(0, str(root / "src"))
        from agent_brain_client.adapters.platforms.filesystem import native_platform

        receipt = dict(
            schema=1,
            mode="local",
            github_actions="not-used",
            scope=scope,
            **before,
            python=platform.python_version(),
            system=sys.platform,
            machine=native_platform(),
            result="passed",
            checks=results,
            native_acceptance="not-checked",
        )
        if started_checks is not None:
            elapsed = time.monotonic() - started_checks
            if elapsed > 300:
                raise ValueError(f"Release preflight took {elapsed:.3f}s and exceeded the 5-minute budget")
            receipt["seconds"] = round(elapsed, 3)
        (output / "checks.json").write_bytes(encoded(receipt))
        return receipt
    return {"scope": scope, "result": "passed", "recorded": False}


def load_receipt(path, tree):
    path = Path(path)
    row = json.loads(path.read_bytes())
    scope = row["scope"]
    if (
        row["schema"] != 1
        or row["mode"] != "local"
        or row["github_actions"] != "not-used"
        or row["source_tree"] != tree
        or row["result"] != "passed"
        or scope not in COMMANDS
    ):
        raise ValueError("Local check receipt does not match the release tree")
    python = tuple(map(int, row["python"].split(".")[:2]))
    if scope == "release":
        if row["system"] not in ("darwin", "win32") or python < (3, 11):
            raise ValueError("Native macOS or Windows release result required")
        elapsed = row.get("seconds")
        if type(elapsed) not in (int, float) or not 0 <= elapsed <= 300:
            raise ValueError("Release preflight exceeded the 5-minute budget")
    elif scope == "windows":
        if row["system"] != "win32" or python not in ((3, 11), (3, 13)):
            raise ValueError("Native Windows matrix evidence required")
    elif python != (3, 12) or (scope == "macos" and row["system"] != "darwin"):
        raise ValueError("Native macOS / Python 3.12 evidence required")
    if [check["command"] for check in row["checks"]] != COMMANDS[scope]:
        raise ValueError("Incomplete or outdated local check commands")
    for index, check in enumerate(row["checks"]):
        if check["log"] != f"{index}.log" or check["exit_code"] != 0:
            raise ValueError("Incomplete local check result")
        log = path.parent / check["log"]
        if log.is_symlink() or sha(log.read_bytes()) != check["sha256"]:
            raise ValueError("Local check log changed")
    return (scope, python), row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scope", choices=COMMANDS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--source-root", type=Path, help="Candidate product checkout for release preflight"
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Bind private evidence to clean committed source",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Use focused test selection without complete local evidence",
    )
    args = parser.parse_args()
    if args.source_root and args.scope != "release":
        parser.error("--source-root is only used for release preflight")
    root = args.source_root.resolve() if args.source_root else ROOT
    result = run_checks(args.scope, args.output, args.record, ci=args.ci, root=root)
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "checks"},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
