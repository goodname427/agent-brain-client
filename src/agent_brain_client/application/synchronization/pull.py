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
                retained = self._retained()
                if retained is not None:
                    return retained
                progress["stage"] = "owned-files"
                owned = self.session.mapping.owned_snapshot()
                progress["stage"] = "remote-snapshot"
                progress["offline_denial_epoch"] = self.session.offline_denial_epoch()
                snapshot = self.session.fetch_snapshot()
                return self.session.mapping.apply_snapshot(snapshot, owned, progress)
        except Exception as error:
            return self._failure(progress, error)

    def background_pull(self, *, still_current, catalog_probe=None, replay=False):
        self.session.ensure_refresh_available()
        progress = {"stage": "local-recovery"}
        try:
            with self.session.refresh_transaction(background=True):
                self.session.mapping.recover()
                self.session.sources.reconcile_pending()
                queue = self.session.queue_status(for_mapping=True)
                if replay and queue["pending"]:
                    if not still_current():
                        return {"status": "content-refresh-deferred", "native_changed": False}
                    self.session.replay_writes()
                    self.session.sources.reconcile_pending()
                    queue = self.session.queue_status(for_mapping=True)
                    # A replay can advance the catalog beyond a peer's recent scan.
                    catalog_probe = None
                retained = self._retained(queue)
                if retained is not None:
                    return retained
                baseline = self.session.refresh_baseline()
                _, _, progress["offline_denial_epoch"] = baseline
            progress["stage"] = "remote-snapshot"
            snapshot = self.session.fetch_snapshot(background_baseline=baseline, catalog_probe=catalog_probe)
            current = still_current()
            with self.session.refresh_transaction(background=True):
                if (not current or self.session.refresh_baseline() != baseline
                        or not self.session.mapping.transaction_settled()):
                    return {"status": "content-refresh-deferred", "native_changed": False}
                self.session.sources.reconcile_pending()
                retained = self._retained()
                if retained is not None:
                    return retained
                progress["stage"] = "owned-files"
                owned = self.session.mapping.owned_snapshot()
                return self.session.mapping.apply_snapshot(snapshot, owned, progress)
        except Exception as error:
            return self._failure(progress, error)

    def _retained(self, queue=None):
        if queue is None:
            queue = self.session.queue_status(for_mapping=True)
        imports = self.session.sources.import_status()
        if not (
            queue["pending"] or queue["conflict"] or queue["blocked"]
            or any(imports["counts"].get(v, 0) for v in ("inventoried", "pending", "conflict", "blocked"))
        ):
            return None
        return {
            "status": "content-local-changes-retained",
            "native_changed": False,
            "model_loaded": "unknown",
            "queue": self.session.queue_status(),
            "imports": imports,
            "next_action": "Reconnect to replay exact saved requests; explicitly reconcile conflicts before mapping.",
        }

    def _failure(self, progress, error):
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
            "status": "content-recovery-review-required" if uncertain else "content-refresh-blocked",
            "stage": progress["stage"],
            "reason": reason,
            "native_changed": None if uncertain else False,
            "model_loaded": "unknown",
            "next_action": "The Server save receipt is independent of this local mapping failure. Preserve local files and prior snapshots; inspect the reported refresh stage before retrying. Do not resubmit a saved write with a new request_id.",
        }
