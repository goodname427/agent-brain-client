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

from agent_brain_client.domain.scopes import validate, digest


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


def activate(value, scope):
    """Read back registration and resume, without fencing an already active source."""
    from agent_brain_client.adapters.server.collection import CollectionClient
    from agent_brain_client.adapters.storage.collection import settings_path

    client = CollectionClient(value)
    response = client.wire("memory_global_status", {})
    result = response.get("result", {})
    pin = digest(scope)
    if (
        "error" in response
        or result.get("source_registered") is not True
        or result.get("scope_sha256") != pin
        or result.get("global_profile_id") != scope["profile_id"]
        or result.get("source_device_id") != client.device_id
    ):
        return {
            "status": "global-memory-admission-pending",
            "activated": False,
            "collection_enabled": False,
            "server_error": response.get("error"),
            "preserve_existing_state": True,
            "next_action": "Retry the normal install or update entry; no new authorization is needed.",
            "model_calls": 0,
        }
    response = client.wire("memory_global_resume", {})
    result = response.get("result", {})
    if (
        "error" in response
        or result.get("schema") != 1
        or result.get("stopped") is not False
    ):
        return {
            "status": "global-memory-resume-unconfirmed",
            "activated": False,
            "collection_enabled": False,
            "server_error": response.get("error"),
            "next_action": "Retry the normal install or update entry.",
            "model_calls": 0,
        }
    value.update(
        scope=scope, approved_scope_sha256=pin, server_scope_sha256=pin, enabled=True
    )
    metadata_io.write(settings_path(value["device_home"]), value)
    (settings_path(value["device_home"]).parent / "global.off").unlink(missing_ok=True)
    return {
        "status": "global-memory-configured",
        "scope_sha256": pin,
        "source_device_id": client.device_id,
        "activated": True,
        "collection_enabled": True,
        "historical_import": False,
        "model_calls": 0,
    }


def register(value, packet):
    """Register this enrolled device; retries retain the exact packet identity."""
    from agent_brain_client.adapters.server.collection import CollectionClient

    validate(packet["scope"])
    if packet["schema"] != 2 or packet["approved_scope_sha256"] != digest(
        packet["scope"]
    ):
        raise ValueError("Original device packet required")
    client = CollectionClient(value)
    if packet["client"]["source_device_id"] != client.device_id:
        raise ValueError("Original enrolled device required")
    response = client.wire("memory_global_register", packet, "source_" + digest(packet))
    result = response.get("result", {})
    if (
        "error" in response
        or result.get("source_registered") is not True
        or result.get("scope_sha256") != digest(packet["scope"])
        or result.get("source_device_id") != client.device_id
        or result.get("global_profile_id") != packet["scope"]["profile_id"]
    ):
        return {
            "status": "global-memory-registration-pending",
            "activated": False,
            "server_error": response.get("error"),
            "model_calls": 0,
            "preserve_existing_state": True,
            "next_action": "Retry the normal install or update entry; the saved packet and existing device are reused.",
        }
    return activate(value, packet["scope"])


def configure(args):
    from agent_brain_client.adapters.storage.collection import load, settings_path

    value = load(args.device_home, verify_code=False)
    directory = settings_path(args.device_home).parent
    pending = directory / "pending-scope.json"
    explicit = (
        args.include_root
        or args.isolate_root
        or args.exclude_root
        or args.non_project_root
    )
    packet_path = getattr(args, "activate_packet", None)
    if packet_path:
        previous = metadata_io.private_json(packet_path)
        scope = copy.deepcopy(validate(previous["scope"]))
    elif explicit:
        scope = copy.deepcopy(value["scope"])

        def canonical(root):
            return str(Path(root).expanduser().resolve())

        scope.update(
            include_roots=[
                {"root": canonical(x), "sharing": "personal"} for x in args.include_root
            ]
            + [{"root": canonical(x), "sharing": "project"} for x in args.isolate_root],
            exclude_roots=[canonical(x) for x in args.exclude_root],
            non_project_roots=[canonical(x) for x in args.non_project_root],
        )
    elif pending.exists():
        scope = copy.deepcopy(validate(metadata_io.private_json(pending)))
    else:
        scope = copy.deepcopy(value["scope"])
    if (
        getattr(args, "all_projects", False)
        or not getattr(args, "preserve_scope", False)
        and not (explicit or packet_path or pending.exists())
    ):
        scope["all_projects"] = True
    validate(scope)
    client, state, failure = preflight(value)
    if failure:
        return failure
    # A local placeholder (including an unsuccessfully approved one) is not a
    # Server identity. Bind to the authenticated existing pool before registration.
    scope["profile_id"] = state["global_profile_id"]
    pin = digest(scope)
    if state["source_registered"] and state.get("scope_sha256") == pin:
        return activate(value, scope)
    _, registered = registration_store.registration(args.device_home)
    runtime = registered["runtime"]
    profile = metadata_io.private_json(
        runtime["https_profile"], runtime["https_profile_sha256"]
    )
    packet = {
        "schema": 2,
        "scope": scope,
        "approved_scope_sha256": pin,
        "client": {
            "profile_sha256": runtime["https_profile_sha256"],
            "content_policy_sha256": runtime["content_policy_sha256"],
            "endpoint": profile["endpoint"],
            "directory": str(directory),
            "legacy_projects": {},
            "actor": profile["grant"]["actor"],
            "source_device_id": client.device_id,
            "grant": profile["grant"],
        },
    }
    # Persist intent separately: connection/registration failure must never
    # disable the previous source, remove its approvals, or stop legacy capture.
    metadata_io.write(pending, scope)
    target = directory / ("device-packet-" + digest(packet) + ".json")
    if not target.exists():
        metadata_io.write(target, packet)
    result = register(value, packet)
    if result.get("activated"):
        output = getattr(args, "packet_output", None)
        if output:
            metadata_io.write(metadata_io.canonical(output), packet)
    return dict(
        result,
        packet=str(target),
        packet_sha256=metadata_io.digest(target.read_bytes()),
    )
