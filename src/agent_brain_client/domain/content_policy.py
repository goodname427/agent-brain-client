"""Content policy containment, independent of storage and Harness I/O."""

from agent_brain_client.domain import contract
from agent_brain_client.domain.errors import CacheBlocked


def validate_policy(policy, grant=None):
    if (
        not isinstance(policy, dict)
        or set(policy) != {"schema", "adapter", "kinds", "ids", "dependencies"}
        or policy["schema"] != 1
        or policy["adapter"] != "codex"
    ):
        raise CacheBlocked()
    contract.validate("content_catalog", {"kinds": policy["kinds"]})
    if not isinstance(policy["ids"], dict) or set(policy["ids"]) != set(
        policy["kinds"]
    ):
        raise CacheBlocked()
    try:
        contract.grant_ids(policy["kinds"], policy["ids"])
    except ValueError:
        raise CacheBlocked()
    if not isinstance(policy["dependencies"], dict) or set(policy["dependencies"]) != {
        "commands",
        "python_modules",
    }:
        raise CacheBlocked()
    contract.strings(policy["dependencies"]["commands"])
    contract.strings(policy["dependencies"]["python_modules"])
    if grant and (
        "read" not in grant["scopes"]
        or set(policy["kinds"]) - set(grant["artifact_types"])
    ):
        raise CacheBlocked()
    if grant:
        try:
            contract.grant_ids(grant["artifact_types"], grant.get("ids"))
        except ValueError:
            raise CacheBlocked()
        if not contract.scope_subset(policy["ids"], grant["ids"]):
            raise CacheBlocked()
