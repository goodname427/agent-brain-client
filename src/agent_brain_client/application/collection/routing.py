"""Shared source admission and per-project delegation, independent of a Harness."""

from typing import Protocol
from agent_brain_client.domain.scopes import within, select


class OriginPorts(Protocol):
    def approved(self, value: dict) -> bool: ...
    def canonical(self, cwd: str) -> str: ...
    def discover(self, cwd: str) -> dict: ...


def eligible(value, cwd, *, ports: OriginPorts):
    if not ports.approved(value):
        return None
    scope = value["scope"]
    if any(within(ports.canonical(cwd), denied) for denied in scope["exclude_roots"]):
        return None
    if not scope.get("all_projects") and not any(
        within(ports.canonical(cwd), root)
        for root in [
            *[item["root"] for item in scope["include_roots"]],
            *scope["non_project_roots"],
        ]
    ):
        return None
    local = ports.discover(cwd)
    return local if select(scope, local) is not None else None


def delegated_project(value, local):
    return next(
        (
            row
            for row in value["legacy_projects"]
            if row["root"] == local["cwd"] and row.get("capture_delegated", True)
        ),
        None,
    )
