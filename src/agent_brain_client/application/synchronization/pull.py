"""Coordinate pull, queue gating and mapping without native filesystem I/O."""

from agent_brain_client.domain.errors import CollectionCapacityExceeded
from agent_brain_client.application.control import RequestStopped
from agent_brain_client.application.synchronization.ports import ContentRefresh


class ContentSynchronization:
    def __init__(self, session: ContentRefresh):
        self.session = session

    def pull(self, *, replay=True):
        self.session.ensure_refresh_available()
        progress = {"stage": "local-recovery"}
        try:
            with self.session.refresh_transaction():
                self.session.mapping.recover()
                self.session.sources.reconcile_pending()
                if replay:
                    self.session.replay_writes()
                self.session.sources.reconcile_pending()
                queue = self.session.queue_status()
                imports = self.session.sources.import_status()
                if (
                    queue["pending"]
                    or queue["conflict"]
                    or queue["blocked"]
                    or any(
                        imports["counts"].get(v, 0)
                        for v in ("inventoried", "pending", "conflict", "blocked")
                    )
                ):
                    return {
                        "status": "content-local-changes-retained",
                        "native_changed": False,
                        "model_loaded": "unknown",
                        "queue": queue,
                        "imports": imports,
                        "next_action": "Reconnect to replay exact saved requests; explicitly reconcile conflicts before mapping.",
                    }
                progress["stage"] = "owned-files"
                owned = self.session.mapping.owned_snapshot()
                progress["stage"] = "remote-snapshot"
                snapshot = self.session.fetch_snapshot()
                return self.session.mapping.apply_snapshot(snapshot, owned, progress)
        except Exception as error:
            uncertain = self.session.mapping.recovery_uncertain()
            reason = (
                "collection-capacity-exceeded"
                if isinstance(error, CollectionCapacityExceeded)
                else "request-stopped"
                if isinstance(error, RequestStopped)
                else "file-access-failed"
                if isinstance(error, OSError)
                else "validation-failed"
            )
            return {
                "status": "content-recovery-review-required"
                if uncertain
                else "content-refresh-blocked",
                "stage": progress["stage"],
                "reason": reason,
                "native_changed": None if uncertain else False,
                "model_loaded": "unknown",
                "next_action": "The Server save receipt is independent of this local mapping failure. Preserve local files and prior snapshots; inspect the reported refresh stage before retrying. Do not resubmit a saved write with a new request_id.",
            }
