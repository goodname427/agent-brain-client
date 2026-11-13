import base64
import json
"""Prepare global source settings without installing a native Harness."""

import os
import uuid
from pathlib import Path
from agent_brain_client.adapters.storage import metadata as metadata_io
from agent_brain_client.adapters.storage import registration as registration_store
from agent_brain_client.adapters.storage import project_settings as project_sources
from agent_brain_client.adapters.platforms.filesystem import private_directory, private_mode
from agent_brain_client.adapters.packages.layout import PYTHON_FILES as FILES
from agent_brain_client.domain.scopes import validate

ROOT = Path(__file__).resolve().parents[4]


def prepare(args, *, locate):
    from agent_brain_client.adapters.storage.collection import settings_path

    home = metadata_io.canonical(args.device_home, existing=True)
    _, registered = registration_store.registration(home)
    file = settings_path(home)
    private_directory(file.parent)
    private_mode(file.parent, directory=True, owner=True)
    if file.exists():
        value = metadata_io.private_json(file)
        validate(value["scope"])
        if value["device_home"] != str(home):
            raise ValueError("Existing global identity preserved")
    else:
        value = {
            "schema": 1,
            "enabled": False,
            "device_home": str(home),
            "scope": {
                "schema": 1,
                "profile_id": uuid.uuid4().hex,
                "include_roots": [],
                "exclude_roots": [],
                "non_project_roots": [],
                "recall_personal": True,
            },
            "approved_scope_sha256": None,
            "server_scope_sha256": None,
            "legacy_projects": [],
            "operator": None,
            "operator_sha256": None,
        }
    profile_id = getattr(args, "profile_id", None)
    if profile_id:
        import re

        if not re.fullmatch("[a-f0-9]{32}", profile_id):
            raise ValueError("Exact existing personal pool identity required")
        if (
            value["scope"]["profile_id"] != profile_id
            and value["server_scope_sha256"] is None
        ):
            value["scope"]["profile_id"] = profile_id
            if value["approved_scope_sha256"] is not None:
                from agent_brain_client.domain.scopes import digest

                value["approved_scope_sha256"] = digest(value["scope"])
        # A registered source is reconciled through authenticated configure,
        # never rebound just because a caller supplied a profile hint.
    routes = {row["root"]: row for row in value["legacy_projects"]}
    base = home / ".config/agent-brain-client/project-memory"
    if os.name != "nt" and base.exists():
        for candidate in base.glob("*/settings.json"):
            try:
                pin = metadata_io.digest(candidate.read_bytes())
                record = metadata_io.private_json(candidate, pin)
                if (project_sources.project_harness(record) != "codex"
                        or record["source_mode"] != "codex-project"):
                    continue
                source = project_sources.load(candidate, pin, locate=locate)
                if source["source_mode"] != "codex-project" or source["harness"] != "codex":
                    continue
                recall = metadata_io.private_json(
                    source["recall_policy"], source["recall_policy_sha256"]
                )
                routes[source["project_root"]] = {
                    **routes.get(source["project_root"], {}),
                    "root": source["project_root"],
                    "settings": str(candidate),
                    "settings_sha256": pin,
                    "project_id": recall["project_id"],
                }
            except (ValueError, OSError):
                continue
    value["legacy_projects"] = list(routes.values())
    value["code_sha256"] = {
        n: metadata_io.digest((ROOT / n).read_bytes()) for n in FILES
    }
    if bool(args.operator) != bool(args.operator_sha256):
        raise ValueError("Complete existing operator reference required")
    if args.operator:
        metadata_io.private_json(args.operator, args.operator_sha256)
        value.update(operator=args.operator, operator_sha256=args.operator_sha256)
    metadata_io.write(file, value)
    return value



def recover_code(home):
    """Restore an interrupted code-pin write, preserving every approval field."""
    from agent_brain_client.adapters.storage.host_files import host_restore
    from agent_brain_client.adapters.storage.collection import (
        settings_path,
        validate_settings,
    )

    file = settings_path(home)
    journal = file.parent / "code-refresh.json"
    if not journal.exists():
        return False
    record = metadata_io.private_json(journal)
    if (
        set(record) != {"schema", "phase", "device_home", "host"}
        or record["schema"] != 1
        or record["device_home"] != str(home)
        or record["phase"] not in ("applying", "complete", "rolled-back")
        or set(record["host"]) != {str(file)}
    ):
        raise ValueError("Global code recovery binding changed")
    row = record["host"][str(file)]
    before, target = (
        validate_settings(
            json.loads(
                base64.b64decode(row[k], validate=True),
                object_pairs_hook=metadata_io.unique,
            ),
            home,
        )
        for k in ("before", "target")
    )
    if {k: v for k, v in before.items() if k != "code_sha256"} != {
        k: v for k, v in target.items() if k != "code_sha256"
    }:
        raise ValueError("Global code recovery must preserve source approval")
    if record["phase"] != "applying":
        return False
    registration_store.registration(home)  # Metadata only; no credential activation.
    host_restore(record["host"], lambda: metadata_io.write(journal, record))
    metadata_io.write(journal, dict(record, phase="rolled-back"))
    return True


def refresh_code(home, source, *, check_only=False):
    from agent_brain_client.adapters.packages.frozen import freeze_code
    from agent_brain_client.adapters.platforms.filesystem import locked
    from agent_brain_client.adapters.storage.collection import settings_path, validate_settings
    from agent_brain_client.adapters.storage.host_files import host_plan, host_apply, host_restore
    file = settings_path(home)
    if not file.exists():
        return False
    if not check_only:
        recover_code(home)
    value = validate_settings(metadata_io.private_json(file), home)
    pins = {name: metadata_io.digest((source / name).read_bytes()) for name in FILES}
    if pins == value["code_sha256"]:
        return False
    if check_only:
        return True
    with locked(file.parent / "code-refresh.lock"):
        original = file.read_bytes()
        freeze_code(file.parent, pins, source=source)
        payload = json.dumps(dict(value, code_sha256=pins), ensure_ascii=False, indent=2).encode() + b"\n"
        journal = file.parent / "code-refresh.json"
        record = {"schema": 1, "phase": "applying", "device_home": str(home),
                  "host": host_plan({file: payload}, private_paths=(file,))}
        if record["host"][str(file)]["before_sha"] != metadata_io.digest(original):
            raise ValueError("Source settings changed before code refresh")
        metadata_io.write(journal, record)
        try:
            host_apply(record["host"], lambda: metadata_io.write(journal, record))
            metadata_io.write(journal, dict(record, phase="complete"))
        except Exception:
            host_restore(record["host"], lambda: metadata_io.write(journal, record))
            metadata_io.write(journal, dict(record, phase="rolled-back"))
            raise
    return True
