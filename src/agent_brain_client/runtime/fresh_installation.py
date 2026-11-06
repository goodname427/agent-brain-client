"""Compose fresh-install ports; installed data stays in its explicit archive."""

from pathlib import Path
from agent_brain_client.adapters.storage.reinstallation import RetainedInstallation
from agent_brain_client.adapters.harnesses.codex.fresh_installation import (
    detach_payload,
    retained_mapping,
    retained_registration,
    restore_mapping,
)
from agent_brain_client.adapters.harnesses.codebuddy.configuration import (
    fresh_installation_payload,
)


def host_payload(home, native, metadata, *, installed=True):
    payload = detach_payload(home, native, metadata, installed=installed)
    payload.update(fresh_installation_payload(home))
    return payload


def retained_installation(home, native):
    return RetainedInstallation(
        Path(home),
        Path(native),
        host_payload=host_payload,
        read_mapping=retained_mapping,
        restore_mapping=restore_mapping,
        read_registration=retained_registration,
        read_project_controls=project_controls,
    )


def project_controls(home):
    from agent_brain_client.adapters.storage.collection import project_references
    from agent_brain_client.adapters.harnesses.codex.project_configuration import (
        retained_controls,
    )

    return retained_controls(home, project_references(home))


def verify_memory(home):
    """Read known retained Memory IDs through the newly authenticated device."""
    from agent_brain_client.adapters.storage.registration import registration
    from agent_brain_client.adapters.server.https import HTTPSContentClient
    from agent_brain_client.adapters.storage.reinstallation import read_journal
    from agent_brain_client.application.installation.fresh import (
        finish_fresh_installation,
    )
    from agent_brain_client.adapters.harnesses.codex.fresh_installation import (
        retained_memory_ids,
    )

    record = read_journal(home)
    if not record or record["phase"] not in ("ready", "complete"):
        return None
    _, policy = registration(home)
    runtime = policy["runtime"]
    client = HTTPSContentClient(
        runtime["https_profile"], runtime["https_profile_sha256"]
    )
    identities = sorted(
        set(retained_memory_ids(record["mapping"])) | set(record.get("memory_ids", []))
    )
    return finish_fresh_installation(
        retained_installation(home, Path(record["native"])),
        client,
        runtime["https_profile_sha256"],
        identities,
    )


def restore_source_routing(home):
    from agent_brain_client.adapters.storage.reinstallation import read_journal
    from agent_brain_client.adapters.storage.collection import (
        seed_retained_routing,
        seed_project_stops,
    )

    record = read_journal(home)
    if (
        record is None
        or record["phase"] != "complete"
        or record.get("routing_restored")
    ):
        return
    if record.get("routing") is not None:
        seed_retained_routing(home, record["routing"])
    seed_project_stops(home, record["project_controls"]["blocked_roots"])
    record["routing_restored"] = True
    retained_installation(home, Path(record["native"])).save(record)
