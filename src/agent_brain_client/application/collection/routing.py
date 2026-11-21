"""Shared source admission and per-project delegation, independent of a Harness."""

from typing import Protocol
from agent_brain_client.domain.scopes import ordinary_drive_path, within, select


class OriginPorts(Protocol):
    def approved(self, value: dict) -> bool: ...
    def canonical(self, cwd: str) -> str: ...
    def discover(self, cwd: str) -> dict: ...


def eligible(value, cwd, *, ports: OriginPorts, prompt=False):
    if not ports.approved(value):
        return None
    scope = value["scope"]
    canonical = ports.canonical(cwd)
    if any(within(canonical, denied) for denied in scope["exclude_roots"]):
        return None
    if not (scope.get("all_projects") or scope.get("all_non_projects")) and not any(
        within(canonical, root)
        for root in [
            *[item["root"] for item in scope["include_roots"]],
            *scope["non_project_roots"],
        ]
    ):
        return None
    local = ports.discover(cwd)
    if select(scope, local) is not None or prompt and select(scope, local, recall=True) is not None:
        return local
    return None


def delegated_project(value, local):
    cwd = ordinary_drive_path(local["cwd"])
    return next(
        (
            row
            for row in value["legacy_projects"]
            if ordinary_drive_path(row["root"]) == cwd and row.get("capture_delegated", True)
        ),
        None,
    )
