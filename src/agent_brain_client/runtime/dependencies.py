"""runtime / dependencies: concrete installation responsibility."""

import sys
from pathlib import Path
import os
import subprocess
from agent_brain_client.adapters.platforms.filesystem import (
    require_platform,
    private_directory,
)
from agent_brain_client.adapters.storage.private_files import private_json, safe
from agent_brain_client.adapters.platforms.credentials import owned
from agent_brain_client.adapters.platforms.macos.helper_build import (
    build as build_helper,
)
import shutil
import ssl

ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT
OFFICIAL_ORIGIN = "https://github.com/goodname427/agent-brain-client.git"
OFFICIAL_REF = "refs/heads/releases/personal-https-stable"


def prepare_dependencies(home, *, runner=subprocess.run, helper_builder=build_helper):
    """Use checked local tools; compile the original helper once at a stable path."""
    from agent_brain_client.adapters.storage.install_state import same_private
    from agent_brain_client.adapters.storage.install_state import sha

    require_platform("first-install")
    if sys.version_info < (3, 11) or not ssl.create_default_context().get_ca_certs():
        raise ValueError("Python 3.11+ and normal TLS roots required")
    python = Path(sys.executable).resolve()
    git_found = shutil.which("git")
    if git_found is None:
        raise ValueError("Git required; rerun the repository bootstrap")
    git = Path(git_found).resolve()
    env = {
        "PATH": os.environ.get("PATH", "") if os.name == "nt" else "/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "en_US.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "0",
    }
    if os.name == "nt":
        env.update(SystemRoot=os.environ["SystemRoot"], USERPROFILE=str(home))
    runner(
        [str(git), "--version"], capture_output=True, timeout=30, env=env, check=True
    )
    core = Path(
        runner(
            [str(git), "--exec-path"],
            capture_output=True,
            timeout=30,
            env=env,
            check=True,
        )
        .stdout.decode()
        .strip()
    ).resolve()
    if not core.is_dir() or not any(
        (core / n).is_file() for n in ("git-remote-https", "git-remote-https.exe")
    ):
        raise ValueError("Git HTTPS transport required")
    state = safe(Path(home) / ".config/agent-brain-client/first-install")
    private_directory(state)
    owned(state, directory=True)
    if os.name == "nt":
        deps = {
            "schema": 2,
            "python": str(python),
            "git": str(git),
            "git_exec_path": str(core),
            "python_sha256": sha(python.read_bytes()),
            "git_sha256": sha(git.read_bytes()),
        }
        same_private(state / "dependencies.json", deps)
        return {
            "source_sha256": sha(
                (
                    SOURCE
                    / "src/agent_brain_client/adapters/platforms/windows/native.py"
                ).read_bytes()
            ),
            "dependencies": deps,
        }
    helper = state / "credential-helper"
    pin = state / "helper.json"
    if helper.exists() or pin.exists():
        metadata = private_json(pin)
        if not helper.is_file() or helper.is_symlink():
            raise ValueError("Existing helper preserved; review incomplete preparation")
        if metadata != {
            "helper_sha256": sha(helper.read_bytes()),
            "source_sha256": sha(
                (
                    SOURCE
                    / "src/agent_brain_client/adapters/platforms/macos/native/CredentialStore.swift"
                ).read_bytes()
            ),
        }:
            raise ValueError("Helper drift; preserve existing credential binding")
    else:
        built = helper_builder(helper)
        metadata = {
            "helper_sha256": built["helper_sha256"],
            "source_sha256": built["source_sha256"],
        }
        same_private(pin, metadata)
    deps = {
        "schema": 2,
        "python": str(python),
        "git": str(git),
        "git_exec_path": str(core),
        "python_sha256": sha(python.read_bytes()),
        "git_sha256": sha(git.read_bytes()),
    }
    same_private(state / "dependencies.json", deps)
    return dict(metadata, credential_helper=str(helper), dependencies=deps)
