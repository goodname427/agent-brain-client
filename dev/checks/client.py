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

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "https://github.com/goodname427/agent-brain-client"
COMMANDS = {
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
        ["sh", "-n", "bootstrap/macos/codebuddy.sh"],
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
            "test_install_contracts.Contracts",
            "test_install_contracts.PlatformMainContracts",
            "test_install_contracts.InstallationArchitectureContracts",
            "test_windows_native",
            "test_windows_private_metadata",
            "test_host_config.WindowsBoundary",
            "test_install_plan",
            "test_source_paths",
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
        ],
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference = 'Stop'; foreach ($file in @('bootstrap/windows/install.ps1', 'bin/memory.ps1', 'bootstrap/windows/codebuddy.ps1', 'bootstrap/windows/python.ps1')) { $errors = $null; [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $pwd $file), [ref]$null, [ref]$errors) | Out-Null; if ($errors.Count) { throw ($errors | Out-String) } }",
        ],
    ],
}


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT).decode().strip()


def identity():
    if git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Recording checks requires a clean committed checkout")
    if git("remote", "get-url", "origin") not in (ORIGIN, ORIGIN + ".git"):
        raise ValueError("Official credential-free Client origin required")
    # Check all public bytes and reject tracked/ignored checkout substitutions.
    import dev.release.client as release_client

    with release_client.source_root(ROOT):
        release_client.source_payload(git("rev-parse", "HEAD"))
    return {
        "source": git("rev-parse", "HEAD"),
        "source_tree": git("rev-parse", "HEAD^{tree}"),
    }


def check_host(scope):
    python = sys.version_info[:2]
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


def run_checks(scope, output, record=False, *, ci=False):
    if ci and record:
        raise ValueError(
            "Focused CI runs cannot produce complete local release evidence"
        )
    check_host(scope)
    before = identity() if record else None
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
                str(ROOT / name)
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
        commands = COMMANDS[scope]
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
                result = subprocess.run(
                    actual,
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=1800,
                )
            results.append(
                {
                    "command": command,
                    "exit_code": result.returncode,
                    "log": log_path.name,
                    "sha256": sha(log_path.read_bytes()),
                }
            )
            if result.returncode:
                print(log_path.read_text(errors="replace"), file=sys.stderr)
                raise ValueError(f"{scope} check failed; inspect {log_path.name}")
    if record:
        if identity() != before:
            raise ValueError("Source changed while checks ran")
        sys.path.insert(0, str(ROOT / "src"))
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
    if scope == "windows":
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


def release_evidence(paths, plan):
    evidence = {}
    for label, tree in (
        ("source", plan["source_tree"]),
        ("executor", plan.get("executor_tree", plan["source_tree"])),
    ):
        rows = {}
        for path in paths:
            raw = json.loads(Path(path).read_bytes())
            if raw.get("source_tree") != tree:
                continue
            key, row = load_receipt(path, tree)
            if key in rows:
                raise ValueError("Duplicate local check evidence")
            rows[key] = row
        required = {
            ("documentation", (3, 12)),
            ("source", (3, 12)),
            ("macos", (3, 12)),
            ("windows", (3, 11)),
            ("windows", (3, 13)),
        }
        if set(rows) != required:
            raise ValueError(
                "Complete local documentation/source/macOS and Windows 3.11/3.13 evidence required: "
                + label
            )
        evidence[label] = [rows[key] for key in sorted(rows)]
    return {
        "mode": "local",
        "github_actions": "not-used",
        "trees": evidence,
        "trust": "operator-retained receipts and logs; not signed CI attestations",
    }


def hybrid_evidence(paths, plan, api):
    """Local Mac scopes and live official Windows job identities stay separate."""
    evidence, checked = {}, {}
    required = {("documentation", (3, 12)), ("source", (3, 12)), ("macos", (3, 12))}
    trees = {plan["source_tree"], plan.get("executor_tree", plan["source_tree"])}
    if any(
        json.loads(Path(path).read_bytes()).get("source_tree") not in trees
        for path in paths
    ):
        raise ValueError("Unexpected local evidence tree")
    for label, tree in (
        ("source", plan["source_tree"]),
        ("executor", plan.get("executor_tree", plan["source_tree"])),
    ):
        if tree not in checked:
            rows = {}
            for path in paths:
                if json.loads(Path(path).read_bytes()).get("source_tree") != tree:
                    continue
                key, row = load_receipt(path, tree)
                if key in rows:
                    raise ValueError("Duplicate local check evidence")
                rows[key] = row
            if set(rows) != required:
                raise ValueError(
                    "Complete local documentation/source/macOS evidence required: "
                    + label
                )
            checked[tree] = {
                "local": [rows[key] for key in sorted(rows)],
                "github_windows": api.windows_ci({"source_tree": tree}),
            }
        evidence[label] = checked[tree]
    return {
        "mode": "hybrid",
        "github_actions": "windows-jobs-only",
        "trees": evidence,
        "trust": "local receipts and logs plus live official GitHub job records; no overall CI success claimed",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scope", choices=COMMANDS)
    parser.add_argument("--output", type=Path, required=True)
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
    result = run_checks(args.scope, args.output, args.record, ci=args.ci)
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "checks"},
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
