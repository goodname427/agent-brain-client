"""Preview/approve sources through an enrolled device or existing SSH operator.

No Server imports, key material, arbitrary remote commands or billing changes.
"""

import agent_brain_client.adapters.storage.registration as registration_store
import agent_brain_client.adapters.storage.metadata as metadata_io
import base64
import copy
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from agent_brain_client.adapters.platforms.filesystem import locked
from agent_brain_client.domain.scopes import validate, digest, has_origins, ordinary_default


def operator(value, packet):
    if not value["operator"] or not value["operator_sha256"]:
        raise ValueError("Existing operator reference required")
    ops = metadata_io.private_json(value["operator"], value["operator_sha256"])
    if (
        set(ops)
        != {
            "schema",
            "ssh_argv",
            "known_hosts",
            "known_hosts_sha256",
            "server_tool_sha256",
        }
        or ops["schema"] != 1
    ):
        raise ValueError("Exact operator reference required")
    argv = ops["ssh_argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or argv[0] not in ("ssh", "/usr/bin/ssh")
        or any(not isinstance(x, str) or "\n" in x or "\x00" in x for x in argv)
        or not any(x == "StrictHostKeyChecking=yes" for x in argv)
        or not any(x.startswith("UserKnownHostsFile=") for x in argv)
        or any(x in ("-A", "-L", "-R", "-D") for x in argv)
    ):
        raise ValueError("Pinned non-forwarding SSH operator required")
    target = urlsplit(packet["client"]["endpoint"]).hostname
    if "@" not in argv[-1] or argv[-1].split("@", 1)[1] != target:
        raise ValueError("Existing operator recipient differs")
    if (
        metadata_io.digest(
            metadata_io.canonical(ops["known_hosts"], existing=True).read_bytes()
        )
        != ops["known_hosts_sha256"]
    ):
        raise ValueError("Existing host verification changed")
    payload = base64.b64encode(json.dumps(packet, ensure_ascii=False).encode()).decode()
    script = """import base64,hashlib,json,subprocess
from pathlib import Path
pid=subprocess.check_output(['systemctl','show','agent-brain.service','--property=MainPID','--value'],text=True).strip()
args=[x.decode() for x in Path('/proc/'+pid+'/cmdline').read_bytes().split(b'\\0') if x]
entry=next(Path(x) for x in args if x.endswith('/scripts/brain_server.py'))
tool=entry.parent.parent/'tools/manage_global_memory.py'
assert hashlib.sha256(tool.read_bytes()).hexdigest()==TOOL_PIN
packet=base64.b64decode(PACKET,validate=True)
r=subprocess.run(['python3','-I','-B',str(tool),'apply'],input=packet,capture_output=True,timeout=200)
try: result=json.loads(r.stdout.splitlines()[-1])
except Exception: result={'status':'global-operator-unconfirmed','preserve_existing_state':True}
print(json.dumps(result))
raise SystemExit(r.returncode)
""".replace("==TOOL_PIN", "==" + repr(ops["server_tool_sha256"])).replace(
        "PACKET,", repr(payload) + ","
    )
    command = (
        "python3 -I -B - <<'AGENT_BRAIN_GLOBAL_OPERATOR'\n"
        + script
        + "AGENT_BRAIN_GLOBAL_OPERATOR\n"
    )
    result = subprocess.run(
        [*argv, "sudo -n /bin/sh -s"],
        input=command,
        capture_output=True,
        text=True,
        timeout=230,
    )
    try:
        receipt = json.loads(result.stdout.splitlines()[-1])
    except (ValueError, IndexError):
        raise ValueError(
            "Global operator outcome unconfirmed; inspect before retry"
        ) from None
    if (
        result.returncode
        or receipt.get("status") != "global-memory-configured"
        or receipt.get("scope_sha256") != digest(packet["scope"])
    ):
        raise ValueError("Global operator did not confirm the requested source policy")
    return receipt


def preflight(value):
    """Read the existing service identity before changing any local source state."""
    from agent_brain_client.adapters.server.collection import CollectionClient

    client = CollectionClient(value)
    response = client.wire("memory_global_status", {})
    result = response.get("result", {})
    import re

    if (
        "error" in response
        or result.get("metadata_only") is not True
        or result.get("source_device_id") != client.device_id
        or type(result.get("source_registered")) is not bool
        or not re.fullmatch("[a-f0-9]{32}", str(result.get("global_profile_id")))
    ):
        return (
            client,
            None,
            {
                "status": "global-memory-connection-pending",
                "activated": False,
                "server_error": response.get("error"),
                "preserve_existing_state": True,
                "next_action": "Retry the normal install or update entry; the existing configuration is retained.",
                "model_calls": 0,
            },
        )
    return client, result, None


def source_result(status, *, next_action=None, **fields):
    result = dict(status=status, collection_enabled=False, activated=False,
                  preserve_existing_state=True, model_calls=0, **fields)
    if next_action is not None:
        result["next_action"] = next_action
    return result


def local_stop(home):
    from agent_brain_client.adapters.storage.collection import settings_path

    off = settings_path(home).parent / "global.off"
    return off.exists() or off.is_symlink()


def paused(*, retained=False, local_fence=False, server=False, pending=False):
    return source_result(
        "retained-source-paused" if retained else "global-memory-source-paused",
        local_stop_fence=local_fence,
        server_stopped=True if server else None,
        next_action=(
            "Use global-configure --activate-packet with the saved device packet to finish this stopped registration."
            if pending else
            "Use explicit global-resume for the approved source, or global-configure to change source settings."
        ),
    )


def matching_registration(state, scope, device_id):
    return (
        state.get("source_registered") is True
        and state.get("scope_sha256") == digest(scope)
        and state.get("global_profile_id") == scope["profile_id"]
        and state.get("source_device_id") == device_id
    )


def report_existing_source(state):
    """Report authenticated Server state without claiming local admission."""
    if state["source_registered"]:
        if state.get("stopped") is True:
            return paused(server=True)
        if state.get("stopped") is False:
            return source_result(
                "global-memory-server-active-local-unconfirmed",
                source_registered=True,
                server_stopped=False,
            )
    return source_result("global-memory-source-preserved")


def activate(value, scope, *, automatic=False, newly_registered=False, state=None):
    """Finalize exact admission, respecting stops on automatic paths."""
    from agent_brain_client.adapters.server.collection import CollectionClient
    from agent_brain_client.adapters.storage.collection import settings_path

    home = value["device_home"]
    client = CollectionClient(value)
    response = (
        client.wire("memory_global_status", {}) if state is None else {"result": state}
    )
    result = response.get("result", {})
    if "error" in response or not matching_registration(result, scope, client.device_id):
        return source_result(
            "global-memory-admission-pending",
            server_error=response.get("error"),
            next_action="Retry the same source setup with the saved packet.",
        )
    if automatic and local_stop(home):
        return paused(local_fence=True)
    if not automatic or result.get("stopped") is not False:
        # Only a newly confirmed registration in this call can explain the
        # Server's initial stop. A later stop has the same status representation.
        if automatic and not newly_registered:
            if result.get("stopped") is True:
                return paused(server=True, pending=True)
            return source_result("global-memory-admission-pending")
        response = client.wire("memory_global_resume", {})
        result = response.get("result", {})
        if (
            "error" in response
            or result.get("schema") != 1
            or result.get("stopped") is not False
        ):
            return source_result(
                "global-memory-resume-unconfirmed",
                server_error=response.get("error"),
                next_action="Retry the same source setup to read back the saved registration.",
            )
    if automatic:
        from agent_brain_client.adapters.storage.collection import load

        # Keep a stop set during registration and avoid overwriting a later
        # source choice. Use current metadata so a code-pin refresh survives.
        latest = load(home, verify_code=False)
        if local_stop(home):
            return paused(local_fence=True)
        if any(latest[key] != value[key] for key in (
            "enabled", "scope", "approved_scope_sha256", "server_scope_sha256",
        )):
            return source_result("global-memory-source-preserved")
        value = latest
    pin = digest(scope)
    value.update(
        scope=scope, approved_scope_sha256=pin, server_scope_sha256=pin, enabled=True
    )
    metadata_io.write(settings_path(value["device_home"]), value)
    if not automatic:
        (settings_path(home).parent / "global.off").unlink(missing_ok=True)
    return {
        "status": "global-memory-configured",
        "scope_sha256": pin,
        "source_device_id": client.device_id,
        "activated": True,
        "collection_enabled": True,
        "historical_import": False,
        "model_calls": 0,
    }


def register(value, packet, *, automatic=False):
    """Register this enrolled device; retries retain the exact packet identity."""
    from agent_brain_client.adapters.server.collection import CollectionClient

    validate(packet["scope"])
    if packet["schema"] != 2 or packet["approved_scope_sha256"] != digest(
        packet["scope"]
    ):
        raise ValueError("Original device packet required")
    if automatic and local_stop(value["device_home"]):
        return paused(local_fence=True)
    client = CollectionClient(value)
    if packet["client"]["source_device_id"] != client.device_id:
        raise ValueError("Original enrolled device required")
    response = client.wire("memory_global_register", packet, "source_" + digest(packet))
    result = response.get("result", {})
    if "error" in response or not matching_registration(
        result, packet["scope"], client.device_id
    ):
        return source_result(
            "global-memory-registration-pending",
            server_error=response.get("error"),
            next_action="Retry the same source setup with the saved packet and device.",
        )
    if automatic:
        return activate(value, packet["scope"], automatic=True, newly_registered=True)
    return activate(value, packet["scope"])


def packet_for(value, scope, client):
    from agent_brain_client.adapters.storage.collection import settings_path

    _, registered = registration_store.registration(value["device_home"])
    runtime = registered["runtime"]
    profile = metadata_io.private_json(
        runtime["https_profile"], runtime["https_profile_sha256"]
    )
    return {
        "schema": 2,
        "scope": scope,
        "approved_scope_sha256": digest(scope),
        "client": {
            "profile_sha256": runtime["https_profile_sha256"],
            "content_policy_sha256": runtime["content_policy_sha256"],
            "endpoint": profile["endpoint"],
            "directory": str(settings_path(value["device_home"]).parent),
            "legacy_projects": {},
            "actor": profile["grant"]["actor"],
            "source_device_id": client.device_id,
            "grant": profile["grant"],
        },
    }


def packet_path(pending, packet):
    return pending.with_name("device-packet-" + digest(packet) + ".json")


def save_packet(pending, packet):
    target = packet_path(pending, packet)
    if target.exists():
        if metadata_io.private_json(target) != packet:
            raise ValueError("Saved source packet changed")
    else:
        metadata_io.write(target, packet)
    return target


def apply_packet(value, pending, packet, *, automatic=False):
    target = save_packet(pending, packet)
    # A recoverable scope always has its matching device packet on disk.
    metadata_io.write(pending, packet["scope"])
    result = register(value, packet, automatic=True) if automatic else register(value, packet)
    return dict(result, packet=str(target), packet_sha256=metadata_io.digest(target.read_bytes()))


def initial_scope(value, retained):
    """Recognize untouched source defaults and the exact retained routing."""
    scope = value["scope"]
    if retained is not None and retained.get("routing") is not None:
        return scope if value["enabled"] and has_origins(scope) else None
    exclusions = retained["project_controls"]["blocked_roots"] if retained else []
    if (
        not value["enabled"]
        and not value["legacy_projects"]
        and "all_projects" not in scope
        and not has_origins(scope)
        and scope["recall_personal"] is True
        and all(root in exclusions for root in scope["exclude_roots"])
    ):
        return dict(scope, all_projects=True, all_non_projects=True)
    return None


def maintain(home, *, initialize=False, retained=None):
    from agent_brain_client.adapters.storage.collection import settings_path

    retained_scope = retained is not None and retained.get("routing") is not None
    if local_stop(home):
        return paused(retained=retained_scope, local_fence=True)
    lock = metadata_io.canonical(settings_path(home).with_name("source-enrollment.lock"))
    with locked(lock):
        return _maintain(home, initialize=initialize, retained=retained)


def _maintain(home, *, initialize, retained):
    from agent_brain_client.adapters.storage.collection import load, settings_path

    retained_scope = retained is not None and retained.get("routing") is not None
    if local_stop(home):
        return paused(retained=retained_scope, local_fence=True)
    value = load(home, verify_code=False)
    bound = (
        value["approved_scope_sha256"] is not None
        or value["server_scope_sha256"] is not None
    )
    if not value["enabled"] and (bound or retained_scope):
        return paused(retained=retained_scope)
    pending = settings_path(home).with_name("pending-scope.json")
    initial = initial_scope(value, retained) if not bound else None
    recover = initial is not None and pending.exists()
    client, state, failure = preflight(value)
    if failure:
        return dict(failure, collection_enabled=False)
    if local_stop(home):
        return paused(retained=retained_scope, local_fence=True)
    if bound:
        return maintain_bound(value, client, state, pending, expand=not initialize)
    if recover:
        saved = validate(metadata_io.private_json(pending))
        if saved["profile_id"] != state["global_profile_id"] or (
            retained is not None and saved != dict(initial, profile_id=saved["profile_id"])
        ):
            return report_existing_source(state)
        packet = packet_for(value, saved, client)
        target = packet_path(pending, packet)
        if not target.exists() or metadata_io.private_json(target) != packet:
            return report_existing_source(state)
        if state["source_registered"]:
            if not matching_registration(state, saved, client.device_id):
                return report_existing_source(state)
            result = activate(value, saved, automatic=True, state=state)
            return dict(result, packet=str(target), packet_sha256=metadata_io.digest(target.read_bytes()))
    else:
        if state["source_registered"] or not initialize or initial is None:
            return report_existing_source(state)
        packet = packet_for(value, dict(initial, profile_id=state["global_profile_id"]), client)
    if local_stop(home):
        return paused(retained=retained_scope, local_fence=True)
    return apply_packet(value, pending, packet, automatic=True)


def maintain_bound(value, client, state, pending, *, expand):
    """Expand only an authenticated default and settle lost replies by readback."""
    scope = value["scope"]
    pin = digest(scope)
    if (
        state.get("stopped") is not False
        or value["approved_scope_sha256"] != pin
        or value["server_scope_sha256"] != pin
        or not has_origins(scope)
    ):
        return report_existing_source(state)
    current = matching_registration(state, scope, client.device_id)
    result = (
        {
            "status": "global-memory-source-current",
            "collection_enabled": True,
            "scope_sha256": pin,
            "source_device_id": client.device_id,
            "model_calls": 0,
        }
        if current else report_existing_source(state)
    )
    if not expand or not ordinary_default(scope):
        return result
    expanded = dict(scope, all_non_projects=True)
    if not current and not matching_registration(state, expanded, client.device_id):
        return result
    if pending.exists() and metadata_io.private_json(pending) not in (scope, expanded):
        return result  # An explicit, unfinished scope choice takes priority.
    packet = dict(packet_for(value, expanded, client), previous_scope_sha256=pin)
    if current:
        save_packet(pending, packet)
        if local_stop(value["device_home"]):
            return paused(local_fence=True)
        reply = client.wire("memory_global_register", packet, "source_" + digest(packet))
        confirmed = reply.get("result", {})
        if not matching_registration(confirmed, expanded, client.device_id):
            error = reply.get("error")
            if error and error.get("code") == "global_source_stopped":
                return paused(server=True)
            return dict(result, ordinary_directory_collection_pending=True,
                        server_error=error)
        state = confirmed
    else:
        # A matching digest alone could belong to a separate explicit setup.
        file = packet_path(pending, packet)
        if not file.exists() or metadata_io.private_json(file) != packet:
            return result
    # This update never calls resume, even if a stop raced the registration.
    if state.get("stopped") is True:
        return paused(server=True)
    return activate(value, expanded, automatic=True, state=state)


def configure(args):
    """Apply an explicitly chosen scope or the original saved packet."""
    from agent_brain_client.adapters.storage.collection import settings_path

    lock = metadata_io.canonical(
        settings_path(args.device_home).with_name("source-enrollment.lock")
    )
    with locked(lock):
        return _configure(args)


def _configure(args):
    from agent_brain_client.adapters.storage.collection import load, settings_path

    value = load(args.device_home, verify_code=False)
    pending = settings_path(args.device_home).with_name("pending-scope.json")
    packet_input = getattr(args, "activate_packet", None)
    all_projects = getattr(args, "all_projects", False)
    all_directories = getattr(args, "all_directories", False)
    scope = copy.deepcopy(value["scope"])
    if packet_input:
        previous = metadata_io.private_json(packet_input)
        scope = validate(previous["scope"])
    else:
        def canonical(root):
            return str(Path(root).expanduser().resolve())

        if all_directories or all_projects or args.include_root or args.isolate_root:
            scope["all_projects"] = all_directories or all_projects
        if all_directories:
            scope["all_non_projects"] = True
        if args.include_root or args.isolate_root:
            scope["include_roots"] = [
                {"root": canonical(x), "sharing": "personal"} for x in args.include_root
            ] + [{"root": canonical(x), "sharing": "project"} for x in args.isolate_root]
        if args.exclude_root:
            scope["exclude_roots"] = [canonical(x) for x in args.exclude_root]
        if args.non_project_root:
            scope["all_non_projects"] = False
            scope["non_project_roots"] = [canonical(x) for x in args.non_project_root]
    validate(scope)
    if not has_origins(scope):
        return source_result(
            "global-memory-activation-required",
            next_action="Choose source roots or pass --all-directories to configure future collection.",
        )
    client, state, failure = preflight(value)
    if failure:
        return failure
    scope = dict(scope, profile_id=state["global_profile_id"])
    packet = packet_for(value, scope, client) if packet_input else None
    if packet_input and previous != packet:
        raise ValueError("Original device and scope packet required")
    if matching_registration(state, scope, client.device_id):
        return activate(value, scope)
    if packet is None:
        packet = packet_for(value, scope, client)
    result = apply_packet(value, pending, packet)
    output = getattr(args, "packet_output", None)
    if result.get("activated") and output:
        metadata_io.write(metadata_io.canonical(output), packet)
    return result
