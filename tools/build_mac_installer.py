"""Build an unpublished arm64 app with reviewed, relocatable Python/Git trees.

No downloads, installation, signing credentials or release publication. Supply
license/provenance metadata and complete prebuilt dependencies explicitly.
"""

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "adapters/codex"),
    str(ROOT / "scripts"),
    str(ROOT / "tools"),
]
from client_release import tree_files
from build_client import export
from build_credential_helper import build as build_helper
from publication_check import require_publishable

LAUNCHER = """#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/../Resources" && pwd -P)
exec "$HERE/dependencies/python/bin/python3" -I -B "$HERE/tools/mac_first_install.py" --bundle "$HERE"
"""


def build(
    output,
    python,
    git,
    provenance,
    *,
    helper_builder=build_helper,
    probe=subprocess.run,
):
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Existing installer preserved")
    provenance = Path(provenance)
    metadata = json.loads(provenance.read_bytes())
    if (
        set(metadata) != {"schema", "platform", "python", "git"}
        or metadata["schema"] != 1
        or metadata["platform"] != "darwin-arm64"
    ):
        raise ValueError("Reviewed dependency provenance required")
    for name in ("python", "git"):
        row = metadata[name]
        if (
            set(row) != {"source", "archive_sha256", "license_files"}
            or urlsplit(row["source"]).scheme != "https"
            or not urlsplit(row["source"]).hostname
            or urlsplit(row["source"]).username
            or urlsplit(row["source"]).password
            or urlsplit(row["source"]).query
            or urlsplit(row["source"]).fragment
            or not re.fullmatch(r"[a-f0-9]{64}", row["archive_sha256"])
            or not row["license_files"]
        ):
            raise ValueError("Dependency source, archive digest and licenses required")
        source = Path(python if name == "python" else git).resolve()
        files = tree_files(source)
        for license_file in row["license_files"]:
            if license_file not in files or "sha256" not in files[license_file]:
                raise ValueError("Dependency licenses missing")
    # The supplied distributions must run after relocation; never use xcrun,
    # CLT installation, a package manager, sudo or a network bootstrap.
    resources = output / "Contents/Resources"
    resources.mkdir(parents=True, mode=0o700)
    try:
        for name, source in (("python", python), ("git", git)):
            shutil.copytree(source, resources / "dependencies" / name, symlinks=True)
        dependencies = resources / "dependencies"
        tree_files(dependencies)  # Validate relative links after relocation.
        env = {
            "PATH": str(dependencies / "git/bin") + ":/usr/bin:/bin",
            "GIT_EXEC_PATH": str(dependencies / "git/libexec/git-core"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(resources),
            "LANG": "en_US.UTF-8",
        }
        run = probe(
            [
                str(dependencies / "python/bin/python3"),
                "-I",
                "-B",
                "-c",
                'import json,platform,ssl,sys,tomllib; print(json.dumps({"platform":platform.system()+"-"+platform.machine(),"version":list(sys.version_info[:2]),"tls":bool(ssl.create_default_context().get_ca_certs())}))',
            ],
            capture_output=True,
            env=env,
            timeout=30,
            check=True,
        )
        check = json.loads(run.stdout)
        if (
            check["platform"] != "Darwin-arm64"
            or tuple(check["version"]) < (3, 11)
            or check["tls"] is not True
        ):
            raise ValueError("Relocatable arm64 Python 3.11+ and TLS roots required")
        probe(
            [str(dependencies / "git/bin/git"), "--version"],
            capture_output=True,
            env=env,
            timeout=30,
            check=True,
        )
        if not (dependencies / "git/libexec/git-core/git-remote-https").is_file():
            raise ValueError("Relocatable Git HTTPS transport required")
        export(ROOT, resources / "client", https_content_candidate=True)
        helper = helper_builder(resources / "credential-helper")
        if helper["self_check"] != {
            "credential_helper_self_check": "passed",
            "keychain_accessed": False,
            "ui_opened": False,
        }:
            raise ValueError("Helper self-check failed")
        (resources / "tools").mkdir()
        shutil.copyfile(
            ROOT / "tools/mac_first_install.py",
            resources / "tools/mac_first_install.py",
        )
        (resources / "dependency-provenance.json").write_bytes(provenance.read_bytes())
        for path in resources.rglob("*"):
            if not path.is_symlink():
                path.chmod(
                    0o700 if path.is_dir() or path.stat().st_mode & 0o111 else 0o600
                )
        manifest = {
            "schema": 1,
            "platform": "darwin-arm64",
            "files": tree_files(resources),
            "credential_helper_sha256": helper["helper_sha256"],
            "credential_source_sha256": helper["source_sha256"],
            "publication": "unpublished",
            "signed": False,
            "notarized": False,
        }
        # Only source/public metadata is scanned as text. Dependency hashes and
        # licenses are reviewed separately; binary payloads never enter Git.
        require_publishable(
            {
                "dependency-provenance.json": provenance.read_bytes(),
                "mac_first_install.py": (
                    ROOT / "tools/mac_first_install.py"
                ).read_bytes(),
            }
        )
        (resources / "bundle-manifest.json").write_text(
            json.dumps(manifest, sort_keys=True) + "\n"
        )
        executable = output / "Contents/MacOS/AgentBrainInstall"
        executable.parent.mkdir()
        executable.write_text(LAUNCHER)
        executable.chmod(0o700)
        (output / "Contents/Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": "org.agentbrain.first-install",
                    "CFBundleName": "Agent Brain Install",
                    "CFBundleExecutable": "AgentBrainInstall",
                    "CFBundlePackageType": "APPL",
                    "LSMinimumSystemVersion": "13.0",
                    "NSHighResolutionCapable": True,
                }
            )
        )
        return {
            "status": "unpublished-mac-installer-built",
            "platform": "darwin-arm64",
            "file_count": len(manifest["files"]),
            "bundle_sha256": hashlib.sha256(
                (resources / "bundle-manifest.json").read_bytes()
            ).hexdigest(),
            "installed": False,
            "published": False,
            "keychain_accessed": False,
            "signed": False,
            "notarized": False,
            "bare_mac_verified": False,
        }
    except Exception:
        # Preserve failed build evidence. A new output is required for retry.
        raise ValueError(
            "Installer build blocked; incomplete output preserved"
        ) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("output", "python", "git", "provenance"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.output, args.python, args.git, args.provenance)))
        return 0
    except Exception:
        print(
            json.dumps(
                {
                    "status": "mac-installer-build-review-required",
                    "published": False,
                    "installed": False,
                }
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
