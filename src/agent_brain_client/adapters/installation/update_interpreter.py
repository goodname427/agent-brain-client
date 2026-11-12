"""Authenticate and re-execute the originally pinned maintenance interpreter."""

import agent_brain_client.adapters.storage.private_files as private_files


import sys
from pathlib import Path
import os
import subprocess
from agent_brain_client.adapters.packages.client import (
    dependency_environment,
    DEPENDENCY_RUNTIME,
)
from agent_brain_client.adapters.storage.private_files import private_json, safe

ROOT = Path(__file__).resolve().parents[4]


def reexec_update_runtime(home, action, *, recovery_context, runner=subprocess.run):
    """Use the originally pinned Python/Git without changing their binding."""
    from agent_brain_client.adapters.installation.installation import (
        inspect,
        registered_context,
    )

    if not inspect(home).get("registration_verified"):
        if action != "update":
            return None
        import agent_brain_client.adapters.installation.installation as content_install

        # Native recovery authenticates its own saved preimage through an
        # injected port. This service never chooses a concrete host.
        recovered = recovery_context()
        if recovered is not None:
            policy, mcp, native = recovered
            return pinned_update_runtime(home, action, policy, mcp, native, runner)
        handover = safe(
            Path(home) / ".config/agent-brain-client/launcher-handover.json"
        )
        if not handover.exists():
            return None
        try:
            saved = private_files.private_json(handover)
            plan = saved["plan"]
            plan_digest = content_install.sha(content_install.contract.encode(plan))
            content_install.validate_install_scope(plan)
            state = Path(home) / ".config/agent-brain-client"
            receipt = private_files.private_json(
                safe(state / "content-install-receipt.json")
            )
            if (
                receipt.get("phase") == "applying"
                and receipt.get("plan_sha256") != plan_digest
            ):
                return None
            meta = private_json(safe(state / "codex-mcp.json"))
            if (
                meta.get("content_candidate") is not True
                or plan.get("device_home") != str(Path(home).resolve())
                or plan.get("release_policy") != meta.get("release_policy")
                or plan.get("release_policy_sha256")
                != meta.get("release_policy_sha256")
            ):
                return None
            from agent_brain_client.adapters.packages.client import policy_file

            policy = policy_file(
                meta["release_policy"],
                meta["release_policy_sha256"],
                runtime_check=False,
            )
            if not DEPENDENCY_RUNTIME <= set(policy["runtime"]):
                return None
            environment = dependency_environment(policy, runtime_check=False)
            dependencies = private_files.private_json(
                safe(policy["runtime"]["dependency_policy"])
            )
            python = str(Path(dependencies["python"]).resolve())
            if plan.get("mcp", {}).get("command") != python:
                return None
            executable = safe(Path(python))
            if not executable.is_file() or not os.access(executable, os.X_OK):
                return None
            if executable.resolve() == Path(sys.executable).resolve():
                return None
            environment = dict(
                environment,
                HOME=str(home),
                CODEX_HOME=plan["codex_home"],
                LANG="en_US.UTF-8",
            )
            return runner(
                [
                    python,
                    "-I",
                    "-B",
                    str(ROOT / "src/agent_brain_client/interfaces/cli/install.py"),
                    "--action",
                    action,
                ],
                env=environment,
                stdin=None
                if action == "update" and sys.stdin.isatty()
                else subprocess.DEVNULL,
            ).returncode
        except Exception:
            return None
    meta = private_json(safe(Path(home) / ".config/agent-brain-client/codex-mcp.json"))
    policy, mcp, _, native = registered_context(home, meta)
    return pinned_update_runtime(home, action, policy, mcp, native, runner)


def pinned_update_runtime(home, action, policy, mcp, native, runner):
    """Re-execute only the registered interpreter with its original bindings."""
    if not DEPENDENCY_RUNTIME <= set(policy["runtime"]):
        return None
    environment = dependency_environment(policy, runtime_check=False)
    if Path(mcp["command"]).resolve() == Path(sys.executable).resolve():
        return None
    environment = dict(
        environment, HOME=str(home), CODEX_HOME=str(native), LANG="en_US.UTF-8"
    )
    return runner(
        [
            mcp["command"],
            "-I",
            "-B",
            str(ROOT / "src/agent_brain_client/interfaces/cli/install.py"),
            "--action",
            action,
        ],
        env=environment,
        stdin=None if action == "update" and sys.stdin.isatty() else subprocess.DEVNULL,
    ).returncode
