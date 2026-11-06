"""Repository bootstrap: fixed Python if needed, then one installer invocation.

Compatible with the macOS toolchain's Python 3.9. No secrets, package manager,
admin credential, arbitrary download URL or system Python replacement.
"""

import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
RUNTIMES = {
    "darwin-arm64": (
        "https://github.com/astral-sh/python-build-standalone/releases/download/20261009/cpython-3.12.15%2B20261009-aarch64-apple-darwin-install_only_stripped.tar.gz",
        "233f8e15255b3e1d1fd47ea5dd5230dfed803f371138e896e54d98c60ae19f6f",
    ),
    "darwin-x86_64": (
        "https://github.com/astral-sh/python-build-standalone/releases/download/20261009/cpython-3.12.15%2B20261009-x86_64-apple-darwin-install_only_stripped.tar.gz",
        "fb3187d95334813b88940db0f1ca57b84cedfd552739c3a5771c3ea0e49988c9",
    ),
}


def dependency(target=None):
    from agent_brain_client.adapters.platforms.filesystem import native_platform

    target = target or native_platform()
    if target not in RUNTIMES:
        raise ValueError("Reviewed native Mac interpreter required")
    return RUNTIMES[target]


def download(path, url):
    with (
        urlopen(url, context=ssl.create_default_context(), timeout=60) as response,
        path.open("xb") as out,
    ):
        if not response.url.startswith("https://"):
            raise ValueError("HTTPS dependency download required")
        count = 0
        while True:
            chunk = response.read(1048576)
            if not chunk:
                break
            count += len(chunk)
            if count > 100 * 1048576:
                raise ValueError("Dependency archive exceeds limit")
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())


def extract(archive, destination, digest):
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise ValueError("Dependency digest mismatch; no downloaded code executed")
    root = destination.resolve()
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if len(members) > 20000 or sum(m.size for m in members) > 1024 * 1048576:
            raise ValueError("Dependency tree exceeds limit")
        for member in members:
            path = root / member.name
            if (
                not member.name.startswith("python/")
                or root not in path.resolve().parents
            ):
                raise ValueError("Dependency path escapes prefix")
            if not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError("Unsupported dependency entry")
            if member.issym() and (
                Path(member.linkname).is_absolute()
                or root not in (path.parent / member.linkname).resolve().parents
            ):
                raise ValueError("Dependency link escapes prefix")
        if hasattr(tarfile, "data_filter"):
            source.extractall(destination, members=members, filter="data")
        else:
            source.extractall(destination, members=members)


def existing_runtime(prefix, digest):
    python = prefix / "python/bin/python3"
    marker = prefix / "archive.sha256"
    if (
        prefix.is_symlink()
        or marker.is_symlink()
        or not marker.is_file()
        or not python.is_file()
        or prefix not in python.resolve().parents
    ):
        raise ValueError("Existing incomplete/drifted runtime preserved")
    if json.loads(marker.read_bytes()) != {
        "archive": digest,
        "python": hashlib.sha256(python.read_bytes()).hexdigest(),
    }:
        raise ValueError("Prepared interpreter drift; preserve runtime")
    return python.resolve()


def runtime(home, *, fetch=None, prepare=True, target=None):
    if sys.version_info >= (3, 11):
        return Path(sys.executable).resolve()
    url, digest = dependency(target)
    directory = Path(home) / ".config/agent-brain-client/install-runtime"
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise ValueError("Canonical dependency directory required")
    if not prepare:
        if (
            not directory.is_dir()
            or directory.stat().st_uid != os.getuid()
            or directory.stat().st_mode & 0o077
        ):
            raise ValueError("Existing private interpreter required for maintenance")
        return existing_runtime(directory / digest, digest)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ValueError("Private dependency directory required")
    sys.path.insert(0, str(ROOT / "src"))
    from agent_brain_client.adapters.platforms.filesystem import locked

    with locked(directory / "prepare.lock", timeout=0):
        prefix = directory / digest
        python = prefix / "python/bin/python3"
        if prefix.exists():
            return existing_runtime(prefix, digest)
        staging = Path(tempfile.mkdtemp(prefix="preparing-", dir=directory))
        archive = staging / "python.tar.gz"
        if fetch is None:
            download(archive, url)
        else:
            fetch(archive)
        unpacked = staging / "prefix"
        unpacked.mkdir(mode=0o700)
        extract(archive, unpacked, digest)
        (unpacked / "archive.sha256").write_text(
            json.dumps(
                {
                    "archive": digest,
                    "python": hashlib.sha256(
                        (unpacked / "python/bin/python3").read_bytes()
                    ).hexdigest(),
                }
            )
        )
        os.rename(unpacked, prefix)
        return python.resolve()


def main():
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from agent_brain_client.adapters.platforms.filesystem import native_platform

        target = native_platform()
        dependency(target)
        sys.path.insert(0, str(ROOT / "src"))
        from bootstrap.source_checkout import should_bootstrap

        prepare_runtime = should_bootstrap(sys.argv[1:])
        python = runtime(Path.home(), prepare=prepare_runtime, target=target)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "LANG": "en_US.UTF-8"}
        if "CODEX_HOME" in os.environ:
            env["CODEX_HOME"] = os.environ["CODEX_HOME"]
        # Only bootstrap switches interpreter; installer never copies/restarts itself.
        return subprocess.run(
            [
                str(python),
                "-I",
                "-B",
                str(ROOT / "src/agent_brain_client/interfaces/cli/install.py"),
                *sys.argv[1:],
            ],
            env=env,
        ).returncode
    except Exception:
        print(
            '{"status":"install-dependencies-review-or-resume-required","installed":false,"credential_accessed":false}'
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
