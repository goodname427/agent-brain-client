"""Run the two verified background operations in finite subprocesses."""

import time

from agent_brain_client.adapters.installation.launcher_update import can_activate_at_startup
from agent_brain_client.adapters.storage import background as state
from agent_brain_client.application.control import RequestControl
from agent_brain_client.domain.scopes import digest


CONTENT_INTERVAL = 60
UPDATE_INTERVAL = 3600


def software_update(home, releases, policy_sha, *, still_current, clock=time.time):
    settings = state.update_policy(home)
    effective = state.policy_state(settings, clock())
    if effective != "enabled":
        return {"status": effective}

    def permitted():
        current = state.update_policy(home)
        return current == settings and state.policy_state(current, clock()) == "enabled" and still_current()

    def activate(active, candidate, manifest):
        return (
            can_activate_at_startup(home, active, candidate, manifest)
            and permitted()
        )

    def perform():
        if not permitted():
            return "policy-changed"
        return releases.update(lock_timeout=0, can_activate=activate)["status"]

    return state.run_due(
        state.directory(home) / "update-attempt.json", digest([policy_sha, settings]),
        UPDATE_INTERVAL, perform, success=("current", "updated"), clock=clock,
    )


def content_refresh(adapter, commit, registration, *, still_current, clock=time.time):
    service = getattr(adapter.client, "queue_identity", adapter.client.grant)
    catalog_identity = digest([adapter.policy, service])

    def probe(fetch):
        # A new release gets a fresh catalog without retaining one path per release.
        return state.shared_catalog(
            adapter.home, catalog_identity, fetch, interval=CONTENT_INTERVAL, clock=clock,
            release=commit,
        )

    def perform():
        if not still_current():
            return "binding-changed"
        control = RequestControl(35)
        try:
            with control.activate():
                return adapter.synchronization.background_pull(
                    still_current=still_current, catalog_probe=probe, replay=True,
                )["status"]
        finally:
            control.close()

    return state.run_due(
        adapter.cache / "background-content.json",
        digest([adapter.queue_sha, commit, registration]), CONTENT_INTERVAL, perform,
        success=("content-current", "content-refreshed"), clock=clock,
    )
