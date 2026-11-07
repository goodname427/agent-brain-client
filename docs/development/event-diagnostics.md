# Capture event diagnostics

Use existing CLI status commands to inspect a single Server capture event without
submitting, retrying, resuming or changing capture. This is not a new MCP tool.
Use matching installed Client entrypoints or an isolated authorized development fixture;
do not substitute the synthetic sandbox or query another device's identity.

For globally collected events, run from the original approved source directory:

```sh
python3 -m agent_brain_client.interfaces.cli.collection global-status --event-id EVENT_ID --session-id SOURCE_SESSION_ID
```

For an existing project configuration with matching installed code pins (an updated
development checkout cannot use older installed project settings until the authorized
update refreshes their pins; never bypass this check):

```sh
python3 -m agent_brain_client.interfaces.cli.project server-status --settings SETTINGS --settings-sha256 SETTINGS_SHA256 --event-id EVENT_ID --session-id SOURCE_SESSION_ID
```

Both flags are required for event diagnosis. `session_id` is the original capture
SOURCE session, not a recall/reader session. Copy the event and source session IDs
from the original capture receipt; do not guess them. Global status uses the current
approved origin. Project status uses the project ID from the pinned existing policy.
The authenticated Server determines device identity; no identity override is offered.
The query uses existing credentials and sends only this selected metadata to the
configured Server. It does not enable capture or widen source scope.

Global output contains `server_event_status.capture_extraction`; project output
contains `capture_extraction`. Ordinary status calls without these flags retain
existing behavior. Event queries do not perform an admission-only status first.
A successful Server response missing diagnostics returns
`event-diagnostics-unavailable` with `reason=server-diagnostics-unsupported`.
An older Server rejecting the new parameter and other failures return
`reason=diagnostics-unconfirmed`, without exposing raw exception details. Neither
means successful extraction. Global diagnosis retains the existing local source
approval gate: a locally stopped or unapproved source is refused, never resumed
automatically for diagnosis. Unknown or unauthorized events must not trigger broader searches.

`state` may be `unknown`, `not-started`, `pending`, `staged`, `paused`, `running`,
`review-required`, `no-facts`, `succeeded` or `continued`. Keep the returned `reason`
and any `original_state`/`original_completed` fields when reporting results.
`running` is recorded state, not proof of a live worker. `continued` preserves an
original unresolved failure, not evidence that it was repaired. `no-facts` does not
prove the source had no useful facts. `succeeded` is workflow completion, not proof
that model output is true. `model_output_truth_verified` remains false. Existing
L0 receipts and `pipeline_state=unverified` are not extraction or recall evidence.
Never use these diagnostics as permission to replay an uncertain model request.
