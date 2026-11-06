# Rule fallback core

> Current development reference; source preparation only. Checked 2026-11-06.

`domain/hook_coverage.py` and `application/memory/rule_fallback.py` under `src/agent_brain_client/` supply a host-independent fallback for missing lifecycle Hooks. It is not imported by an adapter, installed into a Harness, exposed as an MCP tool, or connected to runtime composition. The exact package inventory includes both inert modules, as required by the current architecture; packaging a module does not activate it. This change does not publish or enable a Rule. The existing [Client architecture](architecture.md) remains the transport and storage boundary.

## Ownership

The host reports exactly three active, working Hook capabilities. These are semantic slots; native Hook names remain the host's concern.

| Capability | Hook responsibility | Rule fallback |
| --- | --- | --- |
| `session_start` | Session recall | Call the approved memory query entry before handling the session |
| `user_prompt` | Capture the current user input | Submit a labelled manual observation or Agent summary |
| `assistant_final` | Capture an actual sent reply | Submit an Agent summary before delivery, or a labelled observation after the reply was sent |

`fallback_plan(hooks, rule_enabled=False)` assigns one owner per slot: `hook`, `rule`, or `unavailable`. An available Hook always wins. With all three Hooks, `rule_instructions` returns an empty list even if Rule fallback was opted into. With partial coverage it returns guidance only for missing slots; without Hooks it returns all three. Without explicit opt-in no missing slot runs. There is no combined Hook-plus-Rule mode for the same slot.

Coverage is supplied by a live callback at execution time. Stale guidance cannot override a newly available Hook. Future installation code must remove obsolete guidance and coordinate Hook/Rule transitions with the same execution lock; this core neither discovers native capabilities nor changes native Hook configuration.

## Host contract

Construct `RuleFallback(outbox, hooks=..., enabled=..., source_gate=...)` with the host's existing bound outbox through the typed `RuleCaptureQueue` port. There are no default callbacks. `hooks()` returns all three booleans, `enabled()` returns the explicit Rule opt-in, and `source_gate(capability, source_context)` must return exactly `True` for the current approved source. Missing approvals, exceptions and invalid coverage fail closed. Hosts retain their existing device, principal, origin, grant, future-source and stop/revoke checks. A source gate must not treat a missing native identity as authorization.

`source_context` contains exactly `origin` and `session_id`, following the existing manual memory source contract. Unknown values remain `null`; a future host may reject them when source authorization requires native identity. Never manufacture a native session, project or turn identifier. The outbox owns its atomic cache storage and exclusive `content.lock`; the Rule use case never reads its files or selects a platform lock. Separate host/service bindings need separate caches, as before.

`DurableOutbox.submit_rule_capture` implements the typed port. It stages the exact request, takes its existing `_outbox_locked` cache lock, checks the live route/source predicate, drains only that request and returns the existing receipt. Windows caches retain inherited access descriptors; canonical-path, file-type, reparse/link checks and exclusive locking remain in force. Credentials, policies and executable packages keep strict private-metadata checks. The native Windows regression verifies exact replay with inherited cache ACLs, unchanged directory/lock security and continued rejection by strict private-metadata validation.

`recall(request, source_context=..., invoke=...)` validates an existing `memory_query` request and calls the host's approved recall implementation only when Rule owns `session_start`. The approved implementation must enforce its own grant and actual query scope, validate responses, and preserve its existing recall/provenance semantics. This core does not create a new query protocol, reader identity, endpoint, credential provider, or model context formatter.

`capture(capability, operation_id, text, representation=..., source_context=...)` accepts `user_prompt` or `assistant_final`. Representations are explicit: `agent-summary`, `observed-user-message` for user input, or `observed-assistant-message` for an actually sent reply. Guidance requires an Agent summary before delivery, since a planned final answer is not evidence of a sent reply. Observation labels are caller assertions, not verified native-source proof. Rule adherence, completeness and message truth are not verified.

Capture creates an ordinary `content_put` memory record of type `reference`, with a JSON body tagged `agentbrain.rule.v1`, `source: rule`, capability, representation, local operation ID and text. It never calls `submit_turn`, stages a Hook prompt/final pair, or submits `memory_event_submit`. This preserves the distinction between manual material and native event evidence without changing the Server protocol. Manual records use existing memory storage and derivation; no new memory pipeline is introduced.

## Persistence and retries

The caller creates and retains a stable local `operation_id` for one submission; it is not a native turn ID. The request/target IDs derive from the existing queue binding, source context, capability and operation ID. Text and representation are deliberately excluded from that identity: changing either under the same operation is refused by the shared outbox. Retry the exact original call after a restart. A genuinely new summary requires a new operation ID; do not generate new IDs to bypass a pending or conflicting request.

The existing schema-1 outbox checks grant/target bounds and saves exact bytes before any transfer. Rule requests reserve the `agentbrain_rule_` request-ID prefix. Ordinary refresh/drain skips them, so reconnecting or running another write cannot silently replay Rule text after coverage or source approval changed. An explicit Rule retry uses `exact_request_id` to drain only its own request and rechecks the current route and source gate before network I/O. Unrelated pending writes remain untouched. No timer or automatic replay loop is installed.

Existing queue states, CAS refusal and validated receipts remain authoritative. Pending local data is not Server persistence. Confirmed requests are not resent. Conflict/blocked rows remain retained for review; this core does not automatically rebase them, replace IDs, or retry terminal refusals. After the refusal's cause is fixed, an explicit identical `capture` call can retry a blocked row through the same controlled retry implementation used by ordinary writes. After taking the content lock it revalidates the grant, source and immutable queued request, then checks live Hook/Rule ownership before sending once. A repeated refusal or unconfirmed recovery receipt remains retained; neither a refresh nor a drain retries it. A valid receipt confirms the original request, and a CAS conflict remains a conflict. Revocation retains pending evidence and prevents transfer. The core cannot retract a request already sent before a concurrent change, or detect a host that runs a Hook despite reporting it unavailable.

## Validation and integration boundary

`test_rule_fallback` covers all eight Hook combinations, default opt-out, live Hook takeover, source/grant refusal, explicit representations, unknown/real scope preservation, exact restart/replay, lost acknowledgement, changed-payload refusal, isolated disk persistence and failed durable saves. Blocked-retry regressions cover restored permission after restart, repeated denial, lost recovery acknowledgement, late revocation, immutable payloads and unrelated pending writes. They verify that generic drains never retry Rule refusals and that one explicit call sends at most one retry. Domain coverage tests and a subprocess import check enforce the new inner-layer boundary, without Harness/platform imports or import-path changes. Existing outbox and Hook regressions cover unchanged native event IDs and receipts.

The source is ready for a later host binding. Tool declaration, Rule injection/removal, real native capability detection, operation-ID persistence, transition locking and physical device validation are intentionally left to that separate integration. No concrete Harness has been installed, activated or tested as a Rule host here. Rule instructions can still be missed or interrupted; this is an accepted fallback reliability limit, not complete conversation recording.

There are no aliases, old-path wrappers or second queue implementations. Coverage decisions live in domain; instructions and recall/capture use cases live in application; the existing application queue owns persistence/retry through its typed port; storage and platform adapters retain all I/O and lock implementations. Both native Harnesses, runtime entry points, MCP tools and Rule installation remain unwired.
