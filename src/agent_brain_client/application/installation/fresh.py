"""One-time fresh connection after retaining an old installation's data."""

from typing import Callable, Protocol


class FreshInstallationPort(Protocol):
    def load(self) -> dict | None: ...
    def plan(self) -> dict: ...
    def save(self, record: dict) -> None: ...
    def detach(self, record: dict) -> None: ...
    def archive(self, record: dict) -> None: ...


def prepare_fresh_installation(
    port: FreshInstallationPort,
    confirm: Callable[[str], str],
    *,
    server: str | None = None,
) -> dict:
    record = port.load()
    if record is None:
        record = port.plan()
    if server is not None:
        from agent_brain_client.domain.service import endpoint_check

        if endpoint_check(server) != endpoint_check(record["endpoint"]):
            raise ValueError("Reconnect the SAME Server to preserve memory access")
    if port.load() is None:
        if (
            confirm(
                "Close Codex and CodeBuddy first. Retain old files and unsent queues in the reported archive; "
                "connect a NEW device to the SAME Server/account. Old queues will NOT resume. Type FRESH to continue."
            )
            != "FRESH"
        ):
            return {"status": "fresh-install-cancelled", "installed": False}
        port.save(record)
    if record["phase"] in ("prepared", "detaching"):
        record["phase"] = "detaching"
        port.save(record)
        port.detach(record)
        record["phase"] = "archiving"
        port.save(record)
    if record["phase"] == "archiving":
        port.archive(record)
        record["phase"] = "ready"
        port.save(record)
    if record["phase"] not in ("ready", "complete"):
        raise ValueError("Unknown fresh installation retained")
    return {
        "status": "fresh-install-ready",
        "installed": False,
        "archive": record["archive"],
        "old_queue_replayed": False,
        "old_identity_reused": False,
    }


class MemoryReadPort(Protocol):
    def request(self, request: dict) -> dict: ...


def verify_memory_access(client: MemoryReadPort, identities: list[str]) -> dict:
    from agent_brain_client.domain import contract
    import uuid

    if len(identities) > 1024:
        raise ValueError("Bounded retained memory inventory required")
    requests: list[tuple[str, dict]] = [
        ("content_read", {"kind": "memory", "id": identity}) for identity in identities
    ]
    if not requests:
        requests = [("content_catalog", {"kinds": ["memory"]})]
    for method, params in requests:
        request = {"request_id": uuid.uuid4().hex, "method": method, "params": params}
        response = client.request(request)
        if (
            not isinstance(response, dict)
            or response.get("request_id") != request["request_id"]
            or "error" in response
            or not isinstance(response.get("result"), dict)
        ):
            raise ValueError("Retained Server memory access remains unverified")
        if method == "content_read":
            result = response["result"]
            if result.get("kind") != "memory" or result.get("id") != params["id"]:
                raise ValueError("Retained Memory identity changed")
            contract.record("memory", result["record"], params["id"])
        else:
            contract.validate_catalog(response["result"])
    return {
        "status": "known-memory-readable" if identities else "memory-catalog-readable",
        "verified_records": len(identities),
        "source_scan_performed": False,
        "old_queue_replayed": False,
    }


def finish_fresh_installation(
    port: FreshInstallationPort,
    client: MemoryReadPort,
    profile_sha: str,
    identities: list[str],
) -> dict:
    record = port.load()
    if record is None or record["phase"] not in ("ready", "complete"):
        raise ValueError("Fresh installation is not prepared")
    if profile_sha == record["old_profile_sha256"]:
        raise ValueError("A NEW device profile is required for fresh installation")
    if record["phase"] == "complete":
        if record.get("new_profile_sha256") != profile_sha:
            raise ValueError("Fresh device profile changed after verification")
        return record["memory_accessibility"]
    result = verify_memory_access(client, identities)
    record.update(
        phase="complete", memory_accessibility=result, new_profile_sha256=profile_sha
    )
    port.save(record)
    return result


def validate_reconnection(expected: dict, connection: dict) -> None:
    from agent_brain_client.domain.content_policy import validate_policy

    old, new = expected["grant"], connection["grant"]
    if (
        connection["endpoint"] != expected["endpoint"]
        or new["actor"] != old["actor"]
        or not set(old["scopes"]) <= set(new["scopes"])
    ):
        raise ValueError(
            "Reconnect the SAME Server/account with the original capabilities"
        )
    try:
        validate_policy(expected["content"], new)
    except Exception:
        raise ValueError(
            "Reconnect the SAME Server/account with access to retained memory scope"
        ) from None
