"""Repeatable Mac/Windows release preparation, publication and readback.

Preparation needs only this clean Client checkout. Publication uses the existing
Git authentication and GH_TOKEN, never provisions credentials or changes policy.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    _ENTRY_ROOT = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(_ENTRY_ROOT / "src"), str(_ENTRY_ROOT)]


import argparse
from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from agent_brain_client.adapters.packages.publication import require_publishable

ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT
REPOSITORY = "goodname427/agent-brain-client"
ORIGIN = "https://github.com/" + REPOSITORY + ".git"
CHANNEL = "refs/heads/releases/personal-https-windows-stable"
TRACKING = "refs/remotes/origin/releases/personal-https-windows-stable"
PLATFORMS = ("win32-amd64", "win32-arm64")
MAC_CHANNELS = (
    "refs/heads/releases/personal-https-stable",
    "refs/heads/releases/personal-https-candidate-0.4.0-install-fix-ce9f72b",
)
GROUPS = {
    "windows": {
        "platforms": PLATFORMS,
        "channels": (CHANNEL,),
        "credential": "src/agent_brain_client/adapters/platforms/windows/native.py",
        "label": "Windows",
    },
    "macos": {
        "platforms": ("darwin-arm64", "darwin-x86_64"),
        "channels": MAC_CHANNELS,
        "credential": "src/agent_brain_client/adapters/platforms/macos/native/CredentialStore.swift",
        "label": "macOS",
    },
}
API = "https://api.github.com/repos/" + REPOSITORY
FULL_CI_JOBS = frozenset(
    {
        "changes",
        "documentation",
        "source-checks",
        "isolated-client",
        "windows-contracts (3.11)",
        "windows-contracts (3.13)",
        "Client checks required",
        "Client full validation",
    }
)
HYBRID_CI_JOBS = frozenset(
    {
        "changes",
        "documentation",
        "source-checks",
        "windows-contracts (3.11)",
        "windows-contracts (3.13)",
    }
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def run(args, *, data=None, env=None, cwd=None):
    result = subprocess.run(
        args,
        input=data,
        capture_output=True,
        cwd=ROOT if cwd is None else cwd,
        env=env,
        timeout=600,
    )
    if result.returncode:
        # Subprocess stderr can contain credential-helper output or private paths.
        raise ValueError("Command failed: " + Path(args[0]).name + " " + args[1])
    return result.stdout


def git(*args, data=None, env=None):
    return run(["git", *args], data=data, env=env).decode().strip()


def commit(ref):
    return git("rev-parse", "--verify", ref + "^{commit}")


def version(value):
    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value):
        raise ValueError("Canonical major.minor.patch version required")
    return tuple(map(int, value.split(".")))


def immutable(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError("Existing output differs: " + path.name)
        return
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_bytes(raw)
    os.replace(pending, path)


def report(output, phase, **values):
    value = dict(phase=phase, **values)
    pending = output / "progress.pending"
    pending.write_bytes(encoded(value))
    os.replace(pending, output / "progress.json")
    print(json.dumps(value), flush=True)


@contextmanager
def locked(output):
    output.mkdir(parents=True, exist_ok=True)
    lock = output / "release.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError(
            "Release output is locked; check its owner before removing release.lock"
        ) from None
    try:
        os.write(descriptor, str(os.getpid()).encode())
        os.close(descriptor)
        yield
    finally:
        lock.unlink()


def source_payload(source):
    if commit("HEAD") != source or git("status", "--porcelain"):
        raise ValueError("A clean checkout of the exact source commit is required")
    if git("remote", "get-url", "origin") not in (ORIGIN, ORIGIN.removesuffix(".git")):
        raise ValueError("Official credential-free Client origin required")
    payload, modes = {}, {}
    rows = run(["git", "ls-tree", "-rz", source]).split(b"\0")
    for row in filter(None, rows):
        metadata, name = row.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        name = name.decode()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise ValueError("Linked or external source prohibited: " + name)
        raw = run(["git", "cat-file", "blob", oid])
        path = ROOT / name
        if path.is_symlink() or path.read_bytes() != raw:
            raise ValueError("Checkout bytes differ from source blob: " + name)
        payload[name], modes[name] = raw, int(mode[-3:], 8)
    require_publishable(payload)
    return payload, modes


def archive(payload, prefix, modes=None):
    result = io.BytesIO()
    with gzip.GzipFile(fileobj=result, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(
            fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT
        ) as out:
            for name, raw in sorted(payload.items()):
                item = tarfile.TarInfo(prefix + "/" + name)
                item.size = len(raw)
                item.mode = modes[name] if modes else 0o644
                out.addfile(item, io.BytesIO(raw))
    return result.getvalue()


def require_contract(before, after):
    for key in (
        "schema",
        "channel",
        "core_api",
        "state_schema",
        "python_min",
        "capabilities",
        "migrations",
    ):
        if before[key] != after[key]:
            raise ValueError("Release contract change requires review: " + key)


def compatible(
    before,
    after,
    credential="src/agent_brain_client/adapters/platforms/windows/native.py",
):
    require_contract(before, after)
    if set(before["files"]) != set(after["files"]):
        raise ValueError(
            "Release file scope change requires review; use a new installation for the current layout"
        )
    if before["files"][credential] != after["files"][credential]:
        raise ValueError("Credential component change requires policy review")


def review_reinstallation(before, after, baseline, payload, group):
    """Audit a fresh-install layout change without altering any installed pin."""
    require_contract(before, after)
    credential = GROUPS[group]["credential"]
    prior = {
        "windows": "scripts/brain_windows.py",
        "macos": "native/CredentialStore.swift",
    }[group]
    if credential in before["files"]:
        prior = credential
    if prior not in before["files"] or credential not in after["files"]:
        raise ValueError("Recognized credential component required for reinstallation")
    old = run(["git", "show", baseline + ":" + prior])
    new = payload[credential]
    if sha(old) != before["files"][prior] or sha(new) != after["files"][credential]:
        raise ValueError("Credential audit source integrity failed")
    change = "identical-bytes"
    if old != new:
        old_import = b"from system_credentials import unique\n"
        new_import = (
            b"from agent_brain_client.adapters.platforms.credentials import unique\n"
        )
        if (
            group != "windows"
            or prior != "scripts/brain_windows.py"
            or old.count(old_import) != 1
            or old.replace(old_import, new_import) != new
        ):
            raise ValueError(
                "Credential implementation change requires separate policy review"
            )
        change = "qualified-import-only"
    return {
        "baseline": baseline,
        "prior_version": before["version"],
        "added_files": sorted(set(after["files"]) - set(before["files"])),
        "removed_files": sorted(set(before["files"]) - set(after["files"])),
        "credential": {
            "before": {"path": prior, "sha256": sha(old)},
            "after": {"path": credential, "sha256": sha(new)},
            "change": change,
            "installed_profiles": "preserved-without-repinning",
        },
    }


def runtime_commit(payload, baseline, source, selected, label="Windows"):
    with tempfile.TemporaryDirectory(prefix="client-release-index-") as tmp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(tmp) / "index"))
        git("read-tree", "--empty", env=env)
        for name, raw in sorted(payload.items()):
            oid = git("hash-object", "-w", "--stdin", data=raw)
            git("update-index", "--add", "--cacheinfo", "100644", oid, name, env=env)
        tree = git("write-tree", env=env)
    timestamp = git("show", "-s", "--format=%ct", source) + " +0000"
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Agent Brain Release",
        GIT_AUTHOR_EMAIL="release@example.invalid",
        GIT_COMMITTER_NAME="Agent Brain Release",
        GIT_COMMITTER_EMAIL="release@example.invalid",
        GIT_AUTHOR_DATE=timestamp,
        GIT_COMMITTER_DATE=timestamp,
    )
    message = f"{label} runtime {selected}\n\nSource-Commit: {source}\n"
    parents = (
        (baseline,) if isinstance(baseline, str) else tuple(dict.fromkeys(baseline))
    )
    return git(
        "commit-tree",
        tree,
        *(arg for parent in parents for arg in ("-p", parent)),
        data=message.encode(),
        env=env,
    )


def runtime_artifacts(
    payload, baselines, source, selected, group, *, advance=True, reinstall=False
):
    """Build both native architectures under an explicit upgrade/reinstall policy."""
    definition = GROUPS[group]
    before = [
        json.loads(git("show", parent + ":releases/manifest.json"))
        for parent in baselines
    ]
    if advance and any(
        version(selected) <= version(manifest["version"]) for manifest in before
    ):
        raise ValueError("New version must exceed every selected channel baseline")
    artifacts, manifests, packages = {}, [], []
    with tempfile.TemporaryDirectory(prefix="client-release-build-") as tmp:
        for platform in definition["platforms"]:
            target = Path(tmp) / platform
            # Execute the frozen product checkout's own builder. Its independent
            # repository guard and contract imports remain intact.
            manifest = json.loads(
                run(
                    [
                        sys.executable,
                        "-B",
                        str(ROOT / "dev/build/package.py"),
                        "--output",
                        str(target),
                        "--version",
                        selected,
                        "--mode",
                        "personal-content-https",
                        "--platform",
                        platform,
                    ]
                )
            )
            for parent, previous in zip(baselines, before):
                if reinstall:
                    review_reinstallation(previous, manifest, parent, payload, group)
                else:
                    compatible(previous, manifest, definition["credential"])
            package = {
                p.relative_to(target).as_posix(): p.read_bytes()
                for p in target.rglob("*")
                if p.is_file()
            }
            for name, digest in manifest["files"].items():
                if package[name] != payload[name] or sha(package[name]) != digest:
                    raise ValueError("Runtime source integrity failed: " + name)
            run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(target / "src/agent_brain_client/interfaces/mcp/content.py"),
                    "--self-check",
                ]
            )
            archive_payload = {
                name: raw
                for name, raw in package.items()
                if name != "export-manifest.json"
            }
            arch = platform.split("-", 1)[1]
            name = f"agent-brain-client-runtime-{group}-{arch}-{selected}"
            artifacts[name + ".tar.gz"] = archive(archive_payload, name)
            manifests.append(manifest)
            packages.append(package)
    if any(manifest != manifests[0] for manifest in manifests) or any(
        package != packages[0] for package in packages
    ):
        raise ValueError("Architectures require distinct reviewed channel payloads")
    runtime = (
        runtime_commit(packages[0], baselines, source, selected, definition["label"])
        if advance
        else None
    )
    result = artifacts, runtime, sha(json.dumps(manifests[0], sort_keys=True).encode())
    if reinstall:
        return (
            *result,
            [
                review_reinstallation(previous, manifests[0], parent, payload, group)
                for parent, previous in zip(baselines, before)
            ],
        )
    return result


def build(source, baseline, selected):
    payload, modes = source_payload(source)
    before = json.loads(git("show", baseline + ":releases/manifest.json"))
    baseline_source = commit(
        "refs/tags/installer-" + before["version"] + "-windows-setup"
    )
    git("merge-base", "--is-ancestor", baseline_source, source)
    if version(selected) <= version(before["version"]):
        raise ValueError("New version must be greater than the frozen channel baseline")
    artifacts, runtime, manifest_sha = runtime_artifacts(
        payload, (baseline,), source, selected, "windows"
    )
    name = f"agent-brain-client-installer-{selected}-source"
    artifacts[name + ".tar.gz"] = archive(payload, name, modes)
    for kind in ("installer", "runtime"):
        sums = "".join(
            sha(raw) + "  " + name + "\n"
            for name, raw in sorted(artifacts.items())
            if name.endswith(".tar.gz") and kind in name
        )
        artifacts[f"{kind}-{selected}-SHA256SUMS.txt"] = sums.encode()
    plan = {
        "schema": 1,
        "repository": REPOSITORY,
        "source": source,
        "source_tree": git("rev-parse", source + "^{tree}"),
        "baseline": baseline,
        "baseline_source": baseline_source,
        "version": selected,
        "runtime": runtime,
        "channel": CHANNEL,
        "manifest_sha256": manifest_sha,
        "platforms": list(PLATFORMS),
        "tags": {
            "runtime": f"client-{selected}-windows",
            "installer": f"installer-{selected}-windows-setup",
        },
        "assets": {
            name: {"sha256": sha(raw), "size": len(raw)}
            for name, raw in sorted(artifacts.items())
        },
    }
    return plan, artifacts


def validate(output):
    with tempfile.TemporaryDirectory(prefix="client-release-tests-") as tmp:
        home, codex = Path(tmp) / "home", Path(tmp) / "codex"
        home.mkdir()
        codex.mkdir()
        env = dict(
            os.environ,
            HOME=str(home),
            USERPROFILE=str(home),
            CODEX_HOME=str(codex),
            PYTHONPATH=os.pathsep.join(str(ROOT / p) for p in ("src", ".", "tests")),
        )
        commands = (
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
            [sys.executable, "-B", "dev/checks/source.py"],
            ["git", "diff", "--check"],
        )
        for index, command in enumerate(commands):
            with (output / f"check-{index}.log").open("wb") as log:
                result = subprocess.run(
                    command,
                    env=env,
                    cwd=ROOT,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=1800,
                )
            if result.returncode:
                raise ValueError(
                    f"Local check {index} failed; inspect check-{index}.log"
                )


def prepare(output, source, selected):
    existing = output / "plan.json"
    if existing.exists():
        old = json.loads(existing.read_bytes())
        if old["source"] != source or selected not in ("auto", old["version"]):
            raise ValueError(
                "Output belongs to another source/version; use a separate directory"
            )
        baseline, selected = old["baseline"], old["version"]
    else:
        baseline = commit(TRACKING)
        before = json.loads(git("show", baseline + ":releases/manifest.json"))
        if selected == "auto":
            major, minor, patch = version(before["version"])
            selected = f"{major}.{minor}.{patch + 1}"
    report(output, "building", source=source, version=selected)
    plan, artifacts = build(source, baseline, selected)
    immutable(existing, encoded(plan))
    for name, raw in artifacts.items():
        immutable(output / "assets" / name, raw)
    notes = (
        f"Agent Brain Client {selected} — Windows\n\n"
        f"Source: {source}\nRuntime: {plan['runtime']}\n"
        f"Manifest SHA256: {plan['manifest_sha256']}\n\n"
        + git("show", "-s", "--format=%s", source)
        + "\n\nExisting Windows channel ancestry, file scope, capabilities and credential component retained.\n"
        + "Mac channel unchanged by this release. Desktop/ARM64 hardware and model reload acceptance are separate.\n"
    )
    immutable(output / "release-notes.md", notes.encode())
    report(output, "validating", source=source, version=selected)
    validate(output)
    source_payload(source)
    report(
        output,
        "prepared",
        source=source,
        version=selected,
        runtime=plan["runtime"],
        published=False,
        production_catalog="not-checked",
    )


@contextmanager
def source_root(path):
    global ROOT
    previous, ROOT = ROOT, path
    try:
        yield
    finally:
        ROOT = previous


def executor_identity():
    # The release executor and the product source can be different reviewed
    # commits when completing an already published version on another platform.
    with source_root(TOOL_ROOT):
        source = commit("HEAD")
        source_payload(source)
        return source, git("rev-parse", source + "^{tree}")


def build_platforms(
    source,
    baselines,
    selected,
    groups,
    installer_tag=None,
    *,
    installation_mode="upgrade",
):
    if installation_mode not in ("upgrade", "reinstall"):
        raise ValueError("Explicit supported installation mode required")
    reinstall = installation_mode == "reinstall"
    if reinstall and set(groups) != set(GROUPS):
        raise ValueError("Reinstallation release requires all four platforms")
    payload, modes = source_payload(source)
    executor, executor_tree = executor_identity()
    highest = max(
        version(json.loads(git("show", parent + ":releases/manifest.json"))["version"])
        for parent in baselines.values()
    )
    if version(selected) < highest:
        raise ValueError(
            "Shared installer version must not be below the highest frozen platform version"
        )
    for parent in dict.fromkeys(baselines.values()):
        message = git("show", "-s", "--format=%B", parent)
        recorded = re.findall(r"^Source-Commit: ([a-f0-9]{40})$", message, re.M)
        if recorded:
            if len(recorded) != 1:
                raise ValueError("Ambiguous prior source provenance")
            git("merge-base", "--is-ancestor", recorded[0], source)
    # The reviewed legacy Mac channels predate Source-Commit metadata. The
    # current Windows source floor still prevents rolling shared source back.
    if not re.search(
        r"^Source-Commit: [a-f0-9]{40}$",
        git("show", "-s", "--format=%B", baselines[CHANNEL]),
        re.M,
    ):
        raise ValueError("Current Windows source provenance required")
    if not groups or len(set(groups)) != len(groups) or set(groups) - set(GROUPS):
        raise ValueError("Explicit supported platform groups required")
    artifacts, channels, runtimes, manifests, checks, release_assets, tags = (
        {},
        {},
        {},
        {},
        {},
        {},
        {},
    )
    installation_audits = {}
    for group, definition in GROUPS.items():
        parents = tuple(baselines[ref] for ref in definition["channels"])
        options = {"reinstall": True} if reinstall else {}
        built = runtime_artifacts(
            payload,
            parents,
            source,
            selected,
            group,
            advance=group in groups,
            **options,
        )
        packages, runtime, manifest_sha = built[:3]
        if reinstall:
            installation_audits[group] = built[3]
        checks[group] = {
            "platforms": list(definition["platforms"]),
            "manifest_sha256": manifest_sha,
        }
        if group not in groups:
            continue
        kind = "runtime_" + group
        sums = "".join(
            sha(raw) + "  " + name + "\n" for name, raw in sorted(packages.items())
        )
        packages[f"runtime-{group}-{selected}-SHA256SUMS.txt"] = sums.encode()
        release_assets[kind] = sorted(packages)
        artifacts.update(packages)
        tags[kind] = f"client-{selected}-{group}"
        runtimes[group], manifests[group] = runtime, manifest_sha
        for ref in definition["channels"]:
            channels[ref] = {"baseline": baselines[ref], "runtime": runtime}
    name = f"agent-brain-client-installer-{selected}-source.tar.gz"
    raw = archive(payload, name.removesuffix(".tar.gz"), modes)
    artifacts[name] = raw
    sums_name = f"installer-{selected}-SHA256SUMS.txt"
    artifacts[sums_name] = (sha(raw) + "  " + name + "\n").encode()
    release_assets["installer"] = [name, sums_name]
    legacy_tag = f"installer-{selected}-windows-setup"
    if installer_tag is None:
        legacy = (
            run(
                [
                    "git",
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    "refs/tags/" + legacy_tag + "^{commit}",
                ]
            )
            if git_tag_exists(legacy_tag)
            else b""
        )
        if legacy and legacy.decode().strip() != source:
            raise ValueError(
                "Existing installer version has another source; choose a new shared version"
            )
        installer_tag = legacy_tag if legacy else f"installer-{selected}-setup"
        if (
            git_tag_exists(installer_tag)
            and commit("refs/tags/" + installer_tag) != source
        ):
            raise ValueError(
                "Existing installer version has another source; never replace it"
            )
    tags["installer"] = installer_tag
    plan = {
        "schema": 2,
        "repository": REPOSITORY,
        "source": source,
        "source_tree": git("rev-parse", source + "^{tree}"),
        "executor_source": executor,
        "executor_tree": executor_tree,
        "version": selected,
        "groups": list(groups),
        "baselines": baselines,
        "channels": channels,
        "runtimes": runtimes,
        "manifests": manifests,
        "validated_groups": checks,
        "tags": tags,
        "release_assets": release_assets,
        "assets": {
            name: {"sha256": sha(raw), "size": len(raw)}
            for name, raw in sorted(artifacts.items())
        },
    }
    if reinstall:
        plan["installation"] = {
            "mode": "reinstall",
            "automatic_upgrade": False,
            "data_policy": "retain-old-files-and-queues; fresh-device; preserve-memory-access; never-repin",
            "audits": installation_audits,
        }
    return plan, artifacts


def git_tag_exists(tag):
    return (
        subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", "refs/tags/" + tag],
            cwd=ROOT,
            capture_output=True,
            timeout=30,
        ).returncode
        == 0
    )


def prepare_platforms(
    output,
    source,
    selected,
    target,
    *,
    validate_source=True,
    installation_mode="upgrade",
    release_notes=None,
):
    if release_notes is not None:
        if not release_notes.strip():
            raise ValueError("Non-empty release notes required")
        require_publishable({"release-notes.md": release_notes.encode("utf-8")})
    existing = output / "plan.json"
    groups = list(GROUPS) if target == "all" else [target]
    if existing.exists():
        old = json.loads(existing.read_bytes())
        if (
            old.get("schema") != 2
            or old["source"] != source
            or old["groups"] != groups
            or old.get("installation", {}).get("mode", "upgrade") != installation_mode
            or selected not in ("auto", old["version"])
        ):
            raise ValueError("Output belongs to another source/platform/version")
        baselines, selected, installer_tag = (
            old["baselines"],
            old["version"],
            old["tags"]["installer"],
        )
    else:
        baselines = {
            ref: commit(ref.replace("refs/heads/", "refs/remotes/origin/", 1))
            for definition in GROUPS.values()
            for ref in definition["channels"]
        }
        versions = [
            version(
                json.loads(git("show", parent + ":releases/manifest.json"))["version"]
            )
            for parent in baselines.values()
        ]
        if selected == "auto":
            major, minor, patch = max(versions)
            selected = f"{major}.{minor}.{patch + 1}"
        installer_tag = None
    report(output, "building", source=source, version=selected, groups=groups)
    options = (
        {"installation_mode": installation_mode}
        if installation_mode != "upgrade"
        else {}
    )
    plan, artifacts = build_platforms(
        source, baselines, selected, groups, installer_tag, **options
    )
    notes = (
        f"Agent Brain Client {selected}\n\nSource: {source}\n"
        f"Release executor: {plan['executor_source']}\n\n"
        + "\n".join(
            f"{group}: {runtime}; manifest SHA256: {plan['manifests'][group]}"
            for group, runtime in plan["runtimes"].items()
        )
        + "\n\nBoth platform manifests built and checked at the same component version. "
        + (
            "REINSTALLATION REQUIRED: this version cannot update an old layout in place. "
            "Use the new installer with --action reinstall to archive old state and connect a NEW device to the SAME Server/account. "
            "Do not delete state, reset credentials or rewrite profile pins. "
            "Old unsent queues remain in the archive and are NOT replayed; verify retained Memory access after connecting. "
            if installation_mode == "reinstall"
            else "Only selected existing channels are advanced; their ancestry, file scope and credential pins are retained. "
        )
        + "Desktop and worker/model reload acceptance are separate.\n"
    )
    if release_notes is not None:
        notes = (
            "## 更新日志\n\n"
            + release_notes.rstrip()
            + "\n\n## 发布校验信息\n\n"
            + notes
        )
        plan = {**plan, "release_notes_sha256": sha(notes.encode("utf-8"))}
    immutable(existing, encoded(plan))
    for name, raw in artifacts.items():
        immutable(output / "assets" / name, raw)
    immutable(output / "release-notes.md", notes.encode())
    report(output, "validating", source=source, version=selected, groups=groups)
    if validate_source:
        validate(output)
    source_payload(source)
    report(
        output,
        "prepared",
        source=source,
        version=selected,
        runtimes=plan["runtimes"],
        published=False,
        production_catalog="not-checked",
    )


def prepare_local(
    output, source, selected, target, *, installation_mode="upgrade", release_notes=None
):
    from dev.checks.client import run_checks

    if sys.platform != "darwin" or sys.version_info[:2] != (3, 12):
        raise ValueError(
            "Local release preparation requires macOS / Python 3.12, as on Actions"
        )
    if ROOT != TOOL_ROOT:
        raise ValueError(
            "Local preparation requires source and executor in the same checkout"
        )
    source_payload(source)
    git("fetch", "--no-tags", "origin", "main")
    git("merge-base", "--is-ancestor", source, "FETCH_HEAD")
    for definition in GROUPS.values():
        for ref in definition["channels"]:
            git(
                "fetch",
                "--no-tags",
                "origin",
                ref + ":" + ref.replace("refs/heads/", "refs/remotes/origin/", 1),
            )
    # Keep all logs on failure; a partial local run is never publishable evidence.
    receipts = []
    for scope in ("documentation", "source", "macos"):
        directory = output / "local-checks" / scope
        run_checks(scope, directory, record=True)
        receipts.append(str(directory / "checks.json"))
    prepare_platforms(
        output,
        source,
        selected,
        target,
        validate_source=False,
        installation_mode=installation_mode,
        release_notes=release_notes,
    )
    immutable(
        output / "local-preparation.json",
        encoded(
            {
                "mode": "local",
                "github_actions": "not-used",
                "source": source,
                "receipts": receipts,
                "windows_3_11": "not-run",
                "windows_3_13": "not-run",
                "plan_sha256": sha((output / "plan.json").read_bytes()),
                "native_acceptance": "not-checked",
            }
        ),
    )


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Authenticated API redirect rejected")


class ChecksPending(ValueError):
    """An official same-tree full validation is still running."""


class GitHub:
    def request(self, path, method="GET", value=None, raw=None, upload=False):
        token = os.environ.get("GH_TOKEN")
        if not token and method != "GET":
            raise ValueError(
                "GH_TOKEN from existing authorized authentication is required"
            )
        base = "https://uploads.github.com/repos/" + REPOSITORY if upload else API
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-brain-release",
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        data = encoded(value) if value is not None else raw
        if data is not None:
            headers["Content-Type"] = (
                "application/json" if value is not None else "application/octet-stream"
            )
        try:
            with build_opener(NoRedirect()).open(
                Request(base + path, data=data, method=method, headers=headers),
                timeout=120,
            ) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 404 and method == "GET":
                return None
            raise ValueError(
                f"GitHub {method} failed: HTTP {error.code}; read remote state before retry"
            ) from None

    def find_release(self, tag):
        for page in range(1, 101):
            rows = self.request(f"/releases?per_page=100&page={page}")
            for row in rows:
                if row["tag_name"] == tag:
                    return row
            if len(rows) < 100:
                return None
        raise ValueError("Release pagination limit reached")

    def ci(self, plan):
        pending = False
        for page in range(1, 11):
            result = self.request(
                f"/actions/workflows/client-checks.yml/runs?per_page=100&page={page}"
            )
            for row in result["workflow_runs"]:
                head = row["head_sha"]
                if (
                    row.get("status")
                    in ("queued", "in_progress", "waiting", "pending", "requested")
                    and row.get("event") == "workflow_dispatch"
                    and row.get("head_branch") == "main"
                    and (row.get("head_repository") or {}).get("full_name")
                    == REPOSITORY
                ):
                    candidate = self.request("/git/commits/" + head)
                    pending |= candidate["tree"]["sha"] == plan["source_tree"]
                if (
                    row["conclusion"] != "success"
                    or (row.get("head_repository") or {}).get("full_name") != REPOSITORY
                ):
                    continue
                candidate = self.request("/git/commits/" + head)
                if candidate["tree"]["sha"] != plan["source_tree"]:
                    continue
                if row["event"] == "pull_request":
                    prs = row.get("pull_requests", [])
                    if len(prs) != 1 or prs[0]["base"]["ref"] != "main":
                        continue
                    comparison = self.request(
                        "/compare/" + prs[0]["base"]["sha"] + "..." + head
                    )
                    # PR checkout tests a synthetic merge. Only reuse it when
                    # the base contributes no bytes beyond the recorded head.
                    if not comparison or comparison["status"] not in (
                        "ahead",
                        "identical",
                    ):
                        continue
                elif (
                    row["event"] not in ("push", "workflow_dispatch")
                    or row["head_branch"] != "main"
                ):
                    continue
                # A green scoped workflow may have skipped both platforms. Read
                # this exact attempt's jobs, not statuses from a different run.
                if not self.full_ci_jobs(row):
                    continue
                return {
                    "run_id": row["id"],
                    "url": row["html_url"],
                    "head": head,
                    "source_tree": plan["source_tree"],
                    "conclusion": "success",
                }
            if len(result["workflow_runs"]) < 100:
                break
        if pending:
            raise ChecksPending(
                "Official full Client checks for this exact tree are still running"
            )
        raise ValueError(
            "Successful full Client checks for the exact source tree required (PR, main or manual main run)"
        )

    def wait_ci(self, plan, seconds):
        deadline = time.monotonic() + seconds
        while True:
            try:
                return self.ci(plan)
            except ChecksPending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                print(
                    "Waiting for existing same-tree full validation; no new run triggered",
                    flush=True,
                )
                time.sleep(min(15, remaining))

    def full_ci_jobs(self, row):
        jobs = self.attempt_jobs(row)
        return jobs is not None and self.successful_jobs(jobs, FULL_CI_JOBS)

    def attempt_jobs(self, row):
        jobs = []
        for page in range(1, 11):
            result = self.request(
                f"/actions/runs/{row['id']}/attempts/{row['run_attempt']}/jobs?per_page=100&page={page}"
            )
            if not result:
                return None
            jobs.extend(result["jobs"])
            if len(result["jobs"]) < 100:
                break
        else:
            return None
        return jobs

    @staticmethod
    def successful_jobs(jobs, required):
        for name in required:
            matches = [job for job in jobs if job["name"] == name]
            if len(matches) != 1 or matches[0]["conclusion"] != "success":
                return False
        return True

    def windows_ci(self, plan):
        """Read genuine Windows jobs; never certify the overall workflow."""
        for page in range(1, 11):
            result = self.request(
                f"/actions/workflows/client-checks.yml/runs?per_page=100&page={page}"
            )
            for row in result["workflow_runs"]:
                if (row.get("head_repository") or {}).get("full_name") != REPOSITORY:
                    continue
                head = row["head_sha"]
                candidate = self.request("/git/commits/" + head)
                if candidate["tree"]["sha"] != plan["source_tree"]:
                    continue
                if row["event"] == "pull_request":
                    prs = row.get("pull_requests", [])
                    if len(prs) != 1 or prs[0]["base"]["ref"] != "main":
                        continue
                    comparison = self.request(
                        "/compare/" + prs[0]["base"]["sha"] + "..." + head
                    )
                    if not comparison or comparison["status"] not in (
                        "ahead",
                        "identical",
                    ):
                        continue
                elif (
                    row["event"] not in ("push", "workflow_dispatch")
                    or row["head_branch"] != "main"
                ):
                    continue
                jobs = self.attempt_jobs(row)
                if jobs is None or not self.successful_jobs(jobs, HYBRID_CI_JOBS):
                    continue
                selected = [job for job in jobs if job["name"] in HYBRID_CI_JOBS]
                if any(
                    job.get("status") != "completed" or type(job.get("id")) is not int
                    for job in selected
                ):
                    continue
                return {
                    "run_id": row["id"],
                    "run_attempt": row["run_attempt"],
                    "url": row["html_url"],
                    "head": head,
                    "source_tree": plan["source_tree"],
                    "workflow_status": row["status"],
                    "workflow_conclusion": row["conclusion"],
                    "scope": "windows-jobs-only",
                    "jobs": [
                        {
                            key: job[key]
                            for key in ("id", "name", "status", "conclusion")
                        }
                        for job in sorted(selected, key=lambda job: job["name"])
                    ],
                }
            if len(result["workflow_runs"]) < 100:
                break
        raise ValueError(
            "Successful official Windows 3.11/3.13 checks for the exact source tree required"
        )


def assets_for(plan, kind):
    if plan.get("schema") == 2:
        return {name: plan["assets"][name] for name in plan["release_assets"][kind]}
    return {name: row for name, row in plan["assets"].items() if kind in name}


def check_assets(row, expected, *, complete):
    actual = {item["name"]: item for item in row["assets"]}
    if len(actual) != len(row["assets"]) or set(actual) - set(expected):
        raise ValueError("Unexpected release assets retained; review required")
    if complete and set(actual) != set(expected):
        raise ValueError("Release assets incomplete")
    for name, item in actual.items():
        if (
            item.get("digest") != "sha256:" + expected[name]["sha256"]
            or item["size"] != expected[name]["size"]
        ):
            raise ValueError("Existing remote asset differs; never overwrite: " + name)


def remote_ref(ref):
    rows = git("ls-remote", "--refs", "origin", ref).splitlines()
    if not rows:
        return None
    if len(rows) != 1 or rows[0].split()[1] != ref:
        raise ValueError("Ambiguous remote ref")
    return rows[0].split()[0]


def load_bundle(output):
    saved = json.loads((output / "plan.json").read_bytes())
    if saved.get("schema") == 2:
        plan, artifacts = build_platforms(
            saved["source"],
            saved["baselines"],
            saved["version"],
            saved["groups"],
            saved["tags"]["installer"],
            **(
                {"installation_mode": saved["installation"]["mode"]}
                if "installation" in saved
                else {}
            ),
        )
    else:
        plan, artifacts = build(saved["source"], saved["baseline"], saved["version"])
    if "release_notes_sha256" in saved:
        plan = {
            **plan,
            "release_notes_sha256": sha((output / "release-notes.md").read_bytes()),
        }
    if saved != plan:
        raise ValueError("Prepared plan differs from rebuilt source")
    for name, raw in artifacts.items():
        if (output / "assets" / name).read_bytes() != raw:
            raise ValueError("Prepared asset changed: " + name)
    return plan


def targets(plan):
    if plan.get("schema") == 2:
        return {
            **{
                "runtime_" + group: runtime
                for group, runtime in plan["runtimes"].items()
            },
            "installer": plan["source"],
        }
    return {"runtime": plan["runtime"], "installer": plan["source"]}


def channel_targets(plan):
    return (
        plan["channels"]
        if plan.get("schema") == 2
        else {CHANNEL: {"baseline": plan["baseline"], "runtime": plan["runtime"]}}
    )


def preflight_remote(plan):
    selected = channel_targets(plan)
    for ref, row in selected.items():
        if remote_ref(ref) not in (row["baseline"], row["runtime"]):
            raise ValueError(
                "Channel advanced elsewhere; keep this bundle, prepare a new version"
            )
    for ref, baseline in plan.get("baselines", {}).items():
        if ref not in selected and remote_ref(ref) != baseline:
            raise ValueError(
                "Unselected platform advanced; do not regress the shared installer"
            )
    for kind, target in targets(plan).items():
        actual = remote_ref("refs/tags/" + plan["tags"][kind])
        if actual not in (None, target):
            raise ValueError("Existing tag differs; never move: " + plan["tags"][kind])


def download_assets(plan, row, kind):
    check_assets(row, assets_for(plan, kind), complete=True)
    if row["draft"] or row["prerelease"]:
        raise ValueError("Public stable release required")
    for name, expected in assets_for(plan, kind).items():
        url = f"https://github.com/{REPOSITORY}/releases/download/{plan['tags'][kind]}/{name}"
        # Public downloads carry no Authorization header, including redirects.
        with urlopen(
            Request(url, headers={"User-Agent": "agent-brain-release-check"}),
            timeout=120,
        ) as response:
            raw = response.read(expected["size"] + 1)
        if len(raw) != expected["size"] or sha(raw) != expected["sha256"]:
            raise ValueError("Public download differs: " + name)


def check_release_notes(plan, row, kind):
    digest = plan.get("release_notes_sha256")
    # A partial platform delivery may reuse an already published installer.
    if digest and not (kind == "installer" and len(plan.get("groups", [])) == 1):
        if sha(row.get("body", "").encode("utf-8")) != digest:
            raise ValueError("Published release notes differ from prepared bundle")


def verify(output, api, plan):
    for kind, target in targets(plan).items():
        if remote_ref("refs/tags/" + plan["tags"][kind]) != target:
            raise ValueError("Published tag target unconfirmed")
        row = api.find_release(plan["tags"][kind])
        if row is None:
            raise ValueError("Published release not found")
        check_release_notes(plan, row, kind)
        download_assets(plan, row, kind)
    for ref, row in channel_targets(plan).items():
        if remote_ref(ref) != row["runtime"]:
            raise ValueError("Published channel target unconfirmed")
    latest = api.request("/releases/latest")
    if latest is None or latest["tag_name"] != plan["tags"]["installer"]:
        raise ValueError("Latest installer pointer unconfirmed")
    report(
        output,
        "published-and-verified",
        source=plan["source"],
        version=plan["version"],
        **(
            {"runtimes": plan["runtimes"]}
            if plan.get("schema") == 2
            else {"runtime": plan["runtime"]}
        ),
        public_assets_verified=len(plan["assets"]),
        releases={
            kind: f"https://github.com/{REPOSITORY}/releases/tag/{tag}"
            for kind, tag in plan["tags"].items()
        },
        production_catalog="not-checked",
        desktop_acceptance="not-checked",
    )


def publish(output, api, plan, *, local_checks=(), hybrid_checks=False, wait_for_ci=0):
    git("fetch", "--no-tags", "origin", "main")
    git("merge-base", "--is-ancestor", plan["source"], "FETCH_HEAD")
    if plan.get("schema") == 2:
        git("merge-base", "--is-ancestor", plan["executor_source"], "FETCH_HEAD")
    preflight_remote(plan)
    if hybrid_checks:
        from dev.checks.client import hybrid_evidence

        ci = hybrid_evidence(local_checks, plan, api)
    elif local_checks:
        from dev.checks.client import release_evidence

        ci = release_evidence(local_checks, plan)
    else:
        check_ci = (
            (lambda value: api.wait_ci(value, wait_for_ci)) if wait_for_ci else api.ci
        )
        ci = check_ci(plan)
        if plan.get("schema") == 2 and plan["executor_tree"] != plan["source_tree"]:
            ci["executor"] = check_ci({"source_tree": plan["executor_tree"]})
    (output / "ci.json").write_bytes(encoded(ci))
    notes = (output / "release-notes.md").read_bytes()
    require_publishable({"release-notes.md": notes})
    # Validate all existing releases before making any remote change.
    releases = {}
    for kind in targets(plan):
        row = api.find_release(plan["tags"][kind])
        if row:
            check_release_notes(plan, row, kind)
            check_assets(row, assets_for(plan, kind), complete=not row["draft"])
        releases[kind] = row
    report(output, "publishing-tags", version=plan["version"])
    git(
        "push",
        "--atomic",
        "origin",
        *(
            target + ":refs/tags/" + plan["tags"][kind]
            for kind, target in targets(plan).items()
        ),
    )
    for kind, target in targets(plan).items():
        report(output, "publishing-" + kind, version=plan["version"])
        row = releases[kind]
        if row is None:
            row = api.request(
                "/releases",
                "POST",
                {
                    "tag_name": plan["tags"][kind],
                    "target_commitish": target,
                    "name": f"Agent Brain Client {plan['version']} {kind.replace('_', ' ')}"
                    + (" (Windows)" if plan.get("schema") != 2 else ""),
                    "body": notes.decode(),
                    "draft": True,
                    "prerelease": False,
                    "make_latest": "false",
                },
            )
        if row["draft"]:
            names = {item["name"] for item in row["assets"]}
            for name in assets_for(plan, kind):
                if name not in names:
                    api.request(
                        f"/releases/{row['id']}/assets?name=" + quote(name, safe=""),
                        "POST",
                        raw=(output / "assets" / name).read_bytes(),
                        upload=True,
                    )
            row = api.request(f"/releases/{row['id']}")
            check_assets(row, assets_for(plan, kind), complete=True)
            row = api.request(
                f"/releases/{row['id']}",
                "PATCH",
                {"draft": False, "make_latest": "false"},
            )
        check_release_notes(plan, row, kind)
        download_assets(plan, row, kind)
        releases[kind] = row
    report(output, "promoting-channel", version=plan["version"])
    preflight_remote(plan)
    channels = channel_targets(plan)
    pending = {
        ref: row for ref, row in channels.items() if remote_ref(ref) != row["runtime"]
    }
    if plan.get("schema") == 2 and pending:
        # All selected refs move together; each retains its own frozen parent.
        git(
            "push",
            "--atomic",
            *(
                "--force-with-lease=" + ref + ":" + row["baseline"]
                for ref, row in pending.items()
            ),
            "origin",
            *(row["runtime"] + ":" + ref for ref, row in pending.items()),
        )
    elif pending:
        # The generated commit is a direct child of baseline. The lease rejects
        # a concurrent publisher; no unrelated channel commit can be discarded.
        git(
            "push",
            "--force-with-lease=" + CHANNEL + ":" + plan["baseline"],
            "origin",
            plan["runtime"] + ":" + CHANNEL,
        )
    preflight_remote(plan)
    if any(remote_ref(ref) != row["runtime"] for ref, row in channels.items()):
        raise ValueError("Channel changed before latest promotion; review required")
    api.request(
        f"/releases/{releases['installer']['id']}", "PATCH", {"make_latest": "true"}
    )
    verify(output, api, plan)


def main():
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("prepare", "prepare-artifacts", "prepare-local", "publish", "verify"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", help="Exact 40-character source SHA (prepare only)")
    parser.add_argument(
        "--source-root",
        type=Path,
        help="Clean official checkout of the product source; executor is separately verified",
    )
    parser.add_argument(
        "--targets",
        choices=("all", "windows", "macos"),
        default="all",
        help="all publishes both platforms; a single group completes a partial delivery",
    )
    parser.add_argument(
        "--version",
        default="auto",
        help="auto increments the highest frozen platform patch",
    )
    parser.add_argument(
        "--installation-mode",
        choices=("upgrade", "reinstall"),
        default="upgrade",
        help="prepare only: reinstall explicitly audits a new layout for all four platforms; existing pins remain untouched",
    )
    parser.add_argument(
        "--release-notes-file",
        type=Path,
        help="Required for prepare: UTF-8 public Markdown changelog, frozen with the bundle",
    )
    parser.add_argument("--allow-publish", action="store_true")
    parser.add_argument(
        "--wait-for-ci",
        type=int,
        default=0,
        help="Publish only: bounded wait for an existing exact-tree full run; never dispatch checks",
    )
    parser.add_argument(
        "--local-check",
        type=Path,
        action="append",
        default=[],
        help="Operator-retained checks.json plus sibling logs; repeat for every native check",
    )
    parser.add_argument(
        "--hybrid-checks",
        action="store_true",
        help="Explicitly combine local documentation/source/macOS receipts with official same-tree Windows CI jobs",
    )
    args = parser.parse_args()
    prepare_actions = ("prepare", "prepare-artifacts", "prepare-local")
    if args.wait_for_ci < 0 or (
        args.wait_for_ci and (args.action != "publish" or args.local_check)
    ):
        parser.error("--wait-for-ci requires CI publication and a nonnegative timeout")
    if args.installation_mode != "upgrade" and args.action not in prepare_actions:
        parser.error("installation mode is frozen in the prepared bundle")
    if args.action in prepare_actions:
        if args.release_notes_file is None:
            parser.error("prepare requires --release-notes-file")
        release_notes = args.release_notes_file.read_text(encoding="utf-8")
        if not release_notes.strip():
            parser.error("release notes must not be empty")
        require_publishable({"release-notes.md": release_notes.encode("utf-8")})
    elif args.release_notes_file is not None:
        parser.error("release notes are frozen in the prepared bundle")
    if args.source_root:
        ROOT = args.source_root.resolve()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11+ required")
    output = args.output.resolve()
    if args.action == "publish" and not args.allow_publish:
        parser.error(
            "publish requires --allow-publish; prepare is the offline/dry-run path"
        )
    if args.action == "publish" and not os.environ.get("GH_TOKEN"):
        parser.error(
            "publish requires GH_TOKEN from existing authorized authentication"
        )
    if args.local_check and args.action != "publish":
        parser.error(
            "--local-check is only used for explicit local-evidence publication"
        )
    if args.hybrid_checks and (args.action != "publish" or not args.local_check):
        parser.error(
            "--hybrid-checks requires publish and local documentation/source/macOS receipts"
        )
    if args.action in prepare_actions and not re.fullmatch(
        r"[a-f0-9]{40}", args.source or ""
    ):
        parser.error("prepare requires --source with an exact 40-character SHA")
    with locked(output):
        try:
            if args.action in prepare_actions:
                prepare_fn = (
                    prepare_local
                    if args.action == "prepare-local"
                    else prepare_platforms
                )
                prepare_fn(
                    output,
                    args.source,
                    args.version,
                    args.targets,
                    installation_mode=args.installation_mode,
                    release_notes=release_notes,
                    **(
                        {"validate_source": False}
                        if args.action == "prepare-artifacts"
                        else {}
                    ),
                )
            else:
                plan = load_bundle(output)
                if args.action == "publish":
                    publish(
                        output,
                        GitHub(),
                        plan,
                        local_checks=args.local_check,
                        hybrid_checks=args.hybrid_checks,
                        wait_for_ci=args.wait_for_ci,
                    )
                else:
                    verify(output, GitHub(), plan)
        except Exception as error:
            # Keep the last completed phase plus a safe failure category. Re-run
            # the same command; remote state, not this journal, decides recovery.
            previous = (
                json.loads((output / "progress.json").read_bytes())
                if (output / "progress.json").exists()
                else {}
            )
            report(
                output,
                "failed",
                previous_phase=previous.get("phase"),
                error_type=type(error).__name__,
            )
            if isinstance(error, ValueError):
                print(str(error), file=sys.stderr)
            else:
                print(
                    "Operation unconfirmed; inspect remote state and retry the same bundle",
                    file=sys.stderr,
                )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
