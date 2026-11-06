"""Device-wide source policy and admission metadata, without any Harness."""

import re
from pathlib import Path
from agent_brain_client.adapters.storage import metadata as metadata
from agent_brain_client.adapters.storage import registration as registration_store
from agent_brain_client.adapters.platforms.filesystem import private_mode
from agent_brain_client.domain.scopes import validate, digest
from agent_brain_client.adapters.packages.layout import PYTHON_FILES as FILES

ROOT = Path(__file__).resolve().parents[4]


def settings_path(home):
    return Path(home) / ".config/agent-brain-client/global-memory/settings.json"


def validate_settings(value, home):
    """Validate persisted approval without executing an older pinned runtime."""
    keys = {
        "schema",
        "enabled",
        "scope",
        "approved_scope_sha256",
        "server_scope_sha256",
        "device_home",
        "legacy_projects",
        "code_sha256",
        "operator",
        "operator_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) - {"recall"} != keys
        or value["schema"] != 1
        or type(value["enabled"]) is not bool
        or value["device_home"] != str(Path(home))
    ):
        raise ValueError("Exact global memory settings required")
    from agent_brain_client.domain.contract import recall_options

    options = dict(value.get("recall", {}))
    limit = options.pop("maxResults", 5)
    if type(limit) is not int or not 1 <= limit <= 2**31 - 1:
        raise ValueError("Native L1 count required")
    recall_options(options)
    validate(value["scope"])
    pins = value["code_sha256"]
    if (
        not isinstance(pins, dict)
        or set(pins) != set(FILES)
        or any(
            not isinstance(p, str) or not re.fullmatch("[a-f0-9]{64}", p)
            for p in pins.values()
        )
    ):
        raise ValueError("Complete global code pin required")
    return value


def load(home, *, verify_code=True):
    file = settings_path(home)
    directory = metadata.canonical(file.parent, existing=True)
    private_mode(directory, directory=True, owner=True)
    value = validate_settings(metadata.private_json(file), home)
    if verify_code:
        if set(value["code_sha256"]) != set(FILES):
            raise ValueError("Refresh the owned Hook before running the shared runtime")
        for name, pin in value["code_sha256"].items():
            if metadata.digest((ROOT / name).read_bytes()) != pin:
                raise ValueError(
                    "Global source code changed; refresh owned installation"
                )
    registration_store.registration(
        home
    )  # Metadata only; never activates a credential.
    return value


def approved(value):
    pin = digest(value["scope"])
    return (
        value["enabled"]
        and bool(
            value["scope"].get("all_projects")
            or value["scope"]["include_roots"]
            or value["scope"]["non_project_roots"]
        )
        and value["approved_scope_sha256"] == pin
        and value["server_scope_sha256"] == pin
        and not (settings_path(value["device_home"]).parent / "global.off").exists()
    )


def admission_state(value):
    """Explain each local fence without credentials, content reads or writes."""
    pin = digest(value["scope"])
    reasons = []
    if not value["enabled"]:
        reasons.append("capture-disabled")
    if not (
        value["scope"].get("all_projects")
        or value["scope"]["include_roots"]
        or value["scope"]["non_project_roots"]
    ):
        reasons.append("source-scope-empty")
    if value["approved_scope_sha256"] != pin:
        reasons.append("local-source-approval-required")
    if value["server_scope_sha256"] != pin:
        reasons.append("server-source-approval-unconfirmed")
    if (settings_path(value["device_home"]).parent / "global.off").exists():
        reasons.append("local-stop-fence")
    if not reasons:
        next_action = "check-authenticated-server-source-status"
    elif reasons == ["local-stop-fence"]:
        next_action = "explicit-global-resume-for-the-existing-approved-source"
    elif "source-scope-empty" in reasons or "local-source-approval-required" in reasons:
        next_action = "preview-exact-future-source-scope-and-prepare-device-packet"
    elif "server-source-approval-unconfirmed" in reasons:
        next_action = (
            "check-server-and-have-existing-operator-apply-the-saved-device-packet"
        )
    else:
        next_action = "check-server-and-activate-the-original-approved-device-packet"
    return {
        "status": "locally-ready" if not reasons else "configuration-required",
        "reasons": reasons,
        "next_action": next_action,
        "historical_import": False,
        "permissions_changed": False,
    }


def retained_routing(home):
    """Read routing choices independently of old code pins or device approvals."""
    file = settings_path(home)
    if not file.exists():
        off = file.parent / "global.off"
        if off.exists() or off.is_symlink():
            raise ValueError(
                "Preserve the stop fence; original source settings are missing"
            )
        return None
    value = metadata.private_json(file)
    if value.get("schema") != 1 or value.get("device_home") != str(Path(home)):
        raise ValueError("Original source-routing home required")
    if type(value.get("enabled")) is not bool:
        raise ValueError("Original source pause state required")
    scope = validate(value["scope"])
    keys = ("include_roots", "exclude_roots", "non_project_roots", "recall_personal")
    off = metadata.canonical(file.parent / "global.off")
    source = {str(file): metadata.digest(file.read_bytes())}
    if off.exists():
        # Stop controls historically use touch: an elevated Windows token can
        # give the marker an Administrators owner. It is not a credential item.
        # Keep no-follow/single-link/private-ACL checks, and retain its presence.
        private_mode(off)
        if not off.is_file() or off.stat().st_size > 1024:
            raise ValueError("Original bounded stop fence required")
        source[str(off)] = metadata.digest(off.read_bytes())
    return {
        "preferences": {
            **{key: scope[key] for key in keys},
            "all_projects": scope.get("all_projects", False),
            "enabled": value["enabled"],
            "global_off": off.exists(),
        },
        "source": source,
    }


def seed_retained_routing(home, preferences):
    """Retain exclusions/isolation; approvals and pool ID come from the new device."""
    value = load(home, verify_code=False)
    if value["server_scope_sha256"] is not None:
        return  # Never replace a later authenticated source policy.
    if set(preferences) != {
        "include_roots",
        "exclude_roots",
        "non_project_roots",
        "recall_personal",
        "all_projects",
        "enabled",
        "global_off",
    }:
        raise ValueError("Exact retained source preferences required")
    if any(
        type(preferences[k]) is not bool
        for k in ("enabled", "global_off", "all_projects")
    ):
        raise ValueError("Exact retained pause state required")
    scope = dict(
        value["scope"],
        **{k: v for k, v in preferences.items() if k not in ("enabled", "global_off")},
    )
    validate(scope)
    value["scope"] = scope
    value["enabled"] = preferences["enabled"]
    if preferences["global_off"]:
        from agent_brain_client.adapters.storage.host_files import atomic

        off = metadata.canonical(settings_path(home).parent / "global.off")
        if not off.exists():
            atomic(off, b"retained stop fence\n", 0o600)
    metadata.write(settings_path(home), value)
