"""Source reconciliation contract; source owners retain their own ledgers."""

from typing import Protocol


class SourceReconciliation(Protocol):
    def assert_shareable(self, source_keys: list[str]) -> None: ...
    def validate_reconciliation(
        self, source_keys: list[str], kind: str, identity: str, record: dict | None
    ) -> None: ...
    def confirm_reconciliation(
        self, source_keys: list[str], request_id: str
    ) -> None: ...
    def block_reconciliation(self, source_keys: list[str], request_id: str) -> None: ...
