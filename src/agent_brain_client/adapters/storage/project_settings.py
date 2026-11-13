"""Shared project settings, identity pins and source stop controls."""

from pathlib import Path
import time
from agent_brain_client.adapters.storage.registration import registration
from agent_brain_client.adapters.storage.metadata import (
    canonical,
    digest,
    private_json,
)
from agent_brain_client.adapters.platforms.filesystem import (
    private_mode,
)
from agent_brain_client.adapters.packages.layout import PYTHON_FILES as RUNTIME

ROOT = Path(__file__).resolve().parents[4]
ENTRY = ROOT / "src/agent_brain_client/interfaces/cli/project.py"
MARKER = "Agent Brain project memory"


FIELDS = {
    "schema",
    "harness",
    "enabled",
    "project_root",
    "device_home",
    "codex_home",
    "profile",
    "profile_sha256",
    "content_policy",
    "content_policy_sha256",
    "release_policy",
    "release_policy_sha256",
    "cache",
    "off_file",
    "query",
    "max_items",
    "max_context_bytes",
    "code_sha256",
    "capture_policy",
    "capture_policy_sha256",
    "recall_policy",
    "recall_policy_sha256",
    "source_mode",
}


def project_harness(value):
    """The original schema-1 project record explicitly predates other hosts."""
    if isinstance(value, dict) and value.get("schema") == 1:
        if set(value) == FIELDS - {"harness"}:
            return "codex"  # Retained data format; never a runtime adapter default.
        if set(value) == FIELDS and value.get("harness") in ("codex", "codebuddy"):
            return value["harness"]
    raise ValueError("Exact project Harness record required")

def load(path, sha, *, locate):
    p = private_json(path, sha)
    fields = FIELDS
    harness = project_harness(p)
    p = dict(p, harness=harness)
    if (
        not isinstance(p, dict)
        or set(p) != fields
        or p.get("harness") not in ("codex", "codebuddy")
        or p["schema"] != 1
        or type(p["enabled"]) is not bool
    ):
        raise ValueError("Exact memory settings required")
    if (
        p["source_mode"]
        not in (
            "shared-memory",
            "codex-hooks",
            "codex-project",
            "operator-notes",
            "recall-only",
        )
        or type(p["max_items"]) is not int
        or not 1 <= p["max_items"] <= 3
        or type(p["max_context_bytes"]) is not int
        or not 1024 <= p["max_context_bytes"] <= 16384
        or not isinstance(p["query"], str)
        or len(p["query"]) > 1024
        or set(p["code_sha256"]) != set(RUNTIME)
    ):
        raise ValueError("Bounded project memory settings required")
    for n, h in p["code_sha256"].items():
        if digest(canonical(ROOT / n, existing=True).read_bytes()) != h:
            raise ValueError("Memory code changed; refresh the owned project setup")
    project = canonical(p["project_root"], existing=True)
    if not project.is_dir():
        raise ValueError("Existing project directory required")
    canonical(p["off_file"])
    m, policy = registration(p["device_home"])
    r = policy["runtime"]
    native, cache = locate(canonical(p["device_home"]), r, p["harness"])
    if (
        m["release_policy"] != p["release_policy"]
        or m["release_policy_sha256"] != p["release_policy_sha256"]
        or r["https_profile"] != p["profile"]
        or r["https_profile_sha256"] != p["profile_sha256"]
        or r["content_policy"] != p["content_policy"]
        or r["content_policy_sha256"] != p["content_policy_sha256"]
        or (str(cache) if p["harness"] == "codex" else str(canonical(p["device_home"]) / ".config/agent-brain-client/codebuddy/project-outboxes" / digest(str(project).encode()))) != p["cache"]
        or str(native) != p["codex_home"]
    ):
        raise ValueError("Existing identity/destination changed")
    for name in ("capture_policy", "recall_policy"):
        if bool(p[name]) != bool(p[name + "_sha256"]):
            raise ValueError("Complete policy pin required")
        if p[name]:
            private_json(p[name], p[name + "_sha256"])
    if p["source_mode"] == "shared-memory" and any(
        p[n] is not None for n in ("capture_policy", "recall_policy")
    ):
        raise ValueError("Shared recall does not authorize conversation collection")
    if p["source_mode"] == "recall-only" and (
        p["capture_policy"] is not None or p["recall_policy"] is None
    ):
        raise ValueError("Read-only project link required")
    if p["source_mode"] not in ("shared-memory", "recall-only") and (
        p["capture_policy"] is None or p["recall_policy"] is None
    ):
        raise ValueError(
            "Source collection needs an independently authorized complete Server bundle"
        )
    return p


def active(p):
    marker = Path(p["off_file"])
    global_marker = marker.with_name("global-memory.off")
    if (not p["enabled"] or marker.exists() or marker.is_symlink()
            or global_marker.exists() or global_marker.is_symlink()):
        return False
    if p["recall_policy"]:
        value = private_json(p["recall_policy"], p["recall_policy_sha256"])
        if value.get("lifecycle") == "until-disabled":
            return value["enabled"]
        return value["not_before"] <= time.time() < value["expires_at"]
    return True


def retained_controls(home, references, *, isolate_hosts=False):
    """Snapshot source stop metadata without executing old code or credentials."""
    from agent_brain_client.domain.scopes import path as source_path

    home = canonical(home)
    base = canonical(home / ".config/agent-brain-client/project-memory")
    files = (
        {canonical(p): None for p in base.glob("*/settings.json")}
        if base.exists()
        else {}
    )
    ignored = set()
    for row in references:
        file = canonical(row["settings"])
        source_path(row["root"])
        if row["delegated"]:
            files[file] = row["root"]
        else:
            # These project Hooks were intentionally handed over to global
            # capture; their retired markers are not a user stop of that source.
            ignored.add(file)
    if len(files) > 256:
        raise ValueError("Bounded original project controls required")
    states, pins, blocked = [], {}, set()
    for file, expected_root in sorted(files.items()):
        if file in ignored:
            continue
        value = private_json(file)
        if (
            value.get("schema") != 1
            or value.get("device_home") != str(home)
            or type(value.get("enabled")) is not bool
            or value.get("source_mode")
            not in (
                "shared-memory",
                "codex-hooks",
                "codex-project",
                "operator-notes",
                "recall-only",
            )
        ):
            raise ValueError("Original project control scope required")
        root = str(canonical(value["project_root"]))
        source_path(root)
        if expected_root is not None and source_path(root) != source_path(
            expected_root
        ):
            raise ValueError("Original project routing changed")
        off = canonical(value["off_file"])
        stopped = off.exists()
        pins[str(file)] = digest(file.read_bytes())
        if stopped:
            private_mode(off)
            if not off.is_file() or off.stat().st_size > 1024:
                raise ValueError("Bounded private project stop marker required")
            pins[str(off)] = digest(off.read_bytes())
        states.append(
            {
                "settings": str(file),
                "harness": value["harness"] if "harness" in value else "codex",
                "root": root,
                "enabled": value["enabled"],
                "stopped": stopped,
            }
        )
        if (stopped or not value["enabled"]) and value.get("harness") != "codebuddy" and not isolate_hosts:
            blocked.add(root)
    if len(blocked) > 128:
        raise ValueError("Retained project exclusions exceed source scope capacity")
    return {"blocked_roots": sorted(blocked), "states": states, "source": pins,
            **({"host_scoped": True} if isolate_hosts else {})}


def project_settings(home, project, *, harness):
    if harness not in ("codex", "codebuddy"):
        raise ValueError("Explicit project Harness required")
    return (
        canonical(home)
        / ".config/agent-brain-client/project-memory"
        / (("codebuddy-" if harness == "codebuddy" else "") + digest(str(canonical(project, existing=True)).encode())[:24])
        / "settings.json"
    )


def seed_host_stops(home, states, *, include_legacy=False):
    """Preserve project stop intent for its original host only."""
    from agent_brain_client.adapters.storage.metadata import write
    from agent_brain_client.adapters.platforms.filesystem import private_directory
    hosts = {"codex": [], "codebuddy": []}
    for state in states:
        name = state["harness"] if "harness" in state else "codex"
        if name not in hosts:
            raise ValueError("Unknown retained project host")
        if (name == "codebuddy" or include_legacy) and (state["stopped"] or not state["enabled"]):
            hosts[name].append(str(canonical(state["root"])))
    file = Path(home) / ".config/agent-brain-client/project-memory/retained-stops.json"
    if file.exists():
        if private_json(file) != {"schema": 1, "hosts": hosts}:
            raise ValueError("Retained host stops changed")
        return
    private_directory(file.parent)
    write(file, {"schema": 1, "hosts": hosts}, exclusive=True)


def source_active(home, cwd, harness):
    from agent_brain_client.domain.scopes import within
    if harness not in ("codex", "codebuddy"):
        raise ValueError("Explicit source host required")
    file = Path(home) / ".config/agent-brain-client/project-memory/retained-stops.json"
    if not file.exists():
        return True
    value = private_json(file)
    if set(value) != {"schema", "hosts"} or value["schema"] != 1 or set(value["hosts"]) != {"codex", "codebuddy"}:
        raise ValueError("Exact retained source host scope required")
    roots = value["hosts"][harness]
    if not isinstance(roots, list) or len(roots) > 256:
        raise ValueError("Bounded retained source host stops required")
    return not any(within(str(cwd), root) for root in roots)


def clear_host_stop(home, project, harness):
    """An explicit project enable may lift only its own retained local fence."""
    from agent_brain_client.adapters.storage.metadata import write
    file = Path(home) / ".config/agent-brain-client/project-memory/retained-stops.json"
    if not file.exists():
        return
    source_active(home, project, harness)  # Validate the complete ledger first.
    value = private_json(file)
    root = str(canonical(project, existing=True))
    if root in value["hosts"][harness]:
        value["hosts"][harness].remove(root)
        write(file, value)
