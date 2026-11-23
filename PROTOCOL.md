# Client 服务协议 1

> 当前参考。核对基线为 Client 0.8.36 和 Server 0.3.12。安装、更新和恢复见[统一引导](https://github.com/goodname427/agent-brain-client/blob/main/BOOTSTRAP.md)与[日常维护](https://github.com/goodname427/agent-brain-client/blob/main/docs/installation/maintenance.md)。

## HTTPS device requests and local MCP

Codex and CodeBuddy register the local stdio service `agent_brain_server`. The verified worker advertises its authorized `tools/list` after MCP initialization. Device grant, allowed kinds and lifecycle support determine the list. `clientInfo`, `_meta` and MCP JSON-RPC IDs do not authorize requests or identify writes across processes.

The installed Client sends `POST /v1/requests` to the configured HTTPS origin. The Server's device ingress routes that public path to `/v1/device/requests` and authenticates an enrolled device Bearer. The JSON envelope contains `request_id`, `method` and `params`. A request ID is 16–128 characters. Write retries reuse the exact original ID and parameters. The adapter header remains `codex` for both hosts with protocol and state schema at 1. These headers describe compatibility. Server authorization independently checks the device identity, allowed method and target scope.

`memory_query` is the Agent's query tool. The Content calls below use the Server's typed API. Local request ID generation and queue inspection run in the Client. Event capture, source controls and release discovery use their device methods without adding them to the Agent's MCP list. The local stdio interface and the HTTPS request API use separate transports.

## Typed Content methods

All requests retain the stable `request_id`; writes retain
`expected_revision` (`missing` or SHA256). Params are exact:

| Method | Params |
| --- | --- |
| `content_catalog` | `kinds`: nonempty unique list; optional opaque `cursor` from the preceding `next_cursor` |
| `content_read` | `kind`, `id`; optional `include_archived: true` for an exact Rule or Skill over HTTPS |
| `content_put` | `kind`, `id`, `record`, `expected_revision` |
| `content_set_enabled` | existing `rule` / `skill` `kind`, `id`, boolean `enabled`, non-missing `expected_revision` |
| `content_archive`, `content_restore` | existing `rule` / `skill` `kind`, `id`, non-missing `expected_revision` from the opt-in read |

Default personal scope is `read/put`. The Client and Server check approved artifact types and IDs for each request, including the approved personal-owner collection where applicable. `put` permits creation and CAS updates. Legacy `write` is a typed `put` alias and does not grant removal. The shared contract still describes `content_remove` for separately authorized transports. The current HTTPS Client and Server device request boundaries both reject that method. Rule and Skill archive/restore use their separate lifecycle methods.

`content_set_enabled` requires an explicit user enable/disable request and changes only enabled under the Server CAS lock. Ordinary read/put devices still create disabled Rules/Skills and preserve enabled on content_put edits. Activation uses the same finite/owner ID and put checks, durable outbox and persistence receipt; a confirmed save triggers transactional local mapping. Reads remain read-only; other devices pull at startup or explicitly. Server save, GitHub backup, local mapping and model loading are separate outcomes. See [Rule / Skill management](https://github.com/goodname427/agent-brain-client/blob/main/docs/reference/rule-skill-management.md).

Recoverable archive and restore use explicit user requests and the existing HTTPS device put grant. Lifecycle reads and write results include an `archived` boolean. An archived entry returns `record: null` with a restore revision. Restore returns the original record and ID with `enabled: false`. The lifecycle revision may differ from the ordinary edit and activation revision. The opt-in read requires a successful live response. Default reads and catalogs stay active-only and keep their existing response shape.

The current extensible contract recognizes `memory`, `rule`, `skill`; unknown
kinds require a verified contract/adapter release. Each read or write result
includes `content_api_version: 1`, kind, id, revision, and record (or null).
Catalog returns metadata only, plus its service-confirmed commit. Adapters must
detect a changed catalog/revision while fetching and retain the previous local
snapshot rather than apply a mixed snapshot.

Memory records contain title, description, body and type (user/feedback/reference).
They reuse the canonical Core memory source, revisions and deletion markers.
Rule records contain title, body, enabled and trigger (mode always/on-demand/
conditional, patterns). Skill records contain description, enabled, relative text
files with SKILL.md, and declared command/Python-module dependencies. Filenames
cannot escape the Skill bundle. Neither synchronization nor packaging executes
Skill resources or installs dependencies.

Server writes share the existing request journal, marked commits, CAS, recovery
and normal remote-confirmed push. Rule/Skill records have server-selected paths;
clients never submit Git paths or write GitHub directly. Direct external Git
edits are not imported. A successful local commit or cache refresh does not prove
durability or that an already running model loaded the changed content.


## Requests, receipts and recovery

The Client saves Content writes in a private local outbox before transport.
The complete typed parameters, original request ID and CAS revision are atomically
written and the file/directories fsynced. Startup and write boundaries replay those
exact requests after reconnect or restart. Client 0.2.5 requires a matching
request ID, durable=true, state=remote-confirmed, a Git commit and matching content.
Only a client declaring X-Agent-Brain-Write-Receipt: local-durable-v1 accepts
the strict local-confirmed receipt described below; frozen 0.2.6 does not declare it.
For those receipts, confirmed means Server storage, with GitHub backup reported separately.
Memory follows Core's boundary-whitespace/CRLF normalization and opaque metadata
revision; Rule/Skill revisions must match the returned JSON record. Queued means
saved on the device, awaiting a validated Server storage receipt. Lost acknowledgements
never generate a new operation ID or silently change expected_revision.

Queue identity binds the native mapping policy and service/principal/destination,
independently of the Core code repository or private Data remote. Switching the
service identity blocks replay until pending operations are reviewed. No API
credential or Git repository URL is stored in the outbox. Queue and import state
have bounded local capacity. Capacity failures preserve prior state for review.
Confirmed entries and temporary files left by interrupted atomic saves are
retained as evidence. Only completed request files are eligible for replay.

Client 0.2.1 introduced local outbox/import state. Release transactions never
delete or migrate this content cache, including health rollback or interrupted
activation recovery. A separately reviewed fixed startup must reject content
runtimes older than 0.2.1 whenever outbox or imports.json exists, including empty,
damaged or linked evidence. It reports content-outbox-runtime-incompatible (or
content-local-state-unverified if existence cannot be checked), serves no MCP,
and asks to restore a verified compatible client without deleting local state.
The check also applies to check-only and installation checks. Merely publishing
a package with this guard does not update an old fixed startup: changing the
native MCP bootstrap path/installation receipt requires its exact upgrade plan.
Legacy 0.2.0 retains unknown ledger files but ignores pending operations and can
still map remote content. It must not be served through its old entry after new
local state has been created. Compatible startup can resume exact request/CAS
replay after recovery. Existing release hashes are integrity checks, not an
independent digital signature or a new permission grant.

Ordinary `content_catalog`/`content_read` requests fetch fresh authenticated Server
content once, without rebuilding all native mappings before and after the read.
Catalogs return up to 256 authorized entries per page; follow `next_cursor` until absent. The total collection is not limited to 256 entries;
typed records, revisions and scope checks remain required. Mapping refresh and
outbox/import recovery still run at MCP startup and after confirmed writes.
Read responses report `local_refresh.status:read-only` and do not claim model reload.

Full destination inventory starts with `content_catalog` and `kinds`. In the MCP response the Server page is under `structuredContent.result`. Pass its `next_cursor`, when present, as `cursor` with the same `kinds`, even if the current page has no visible artifacts. The last page omits `next_cursor`. The tool accepts no `limit`. Destination cursors pin the collection revision and authorization scope. A changed snapshot requires restarting without a cursor. Use `memory_query` for bounded retrieval and Onboarding for source coverage. Invalid parameters are local JSON-RPC `-32602`, with
`data.stage:parameters`; no adapter or HTTP request is made. Read parameter
errors do not request write-only CAS fields.

Content MCP tool results include `client_runtime.running_worker_version` and,
when its immutable verified package receipt is available, `running_worker_commit`.
This observes the serving worker, not the mutable active pointer. The public
repository check-update/update commands reuse the registered release policy;
CLI active-package and channel-candidate versions do not prove worker reload.

Client HTTP failures expose only safe categories: `transport` (no HTTP status),
`http` (actual status when available), `response-parse` and `response-validation`.
Invalid JSON, mismatched IDs and invalid write receipts are not reported as
network transport failures or served from stale snapshots. Unknown write results
remain retryable with the original request ID and CAS parameters. Raw exception
text, headers and error-body details are omitted; content validation and local
policy/credential failures have separate stages.

The content MCP executor receives cancellation notifications while processing
requests. Read requests have a 90-second total deadline including queue time;
expired or cancelled queued requests do not start transport. Active read I/O is
aborted and late results are discarded, with explicit JSON-RPC errors `-32001`
(deadline) or `-32800` (cancelled). The bounded queue rejects excess work visibly.
Already-started writes finish their durable transaction; an unknown write result
still requires recovery with the original request ID and CAS parameters.

The local MCP adapter may answer `content_catalog`/`content_read` from a prior
verified snapshot only after an unconfirmed transport failure. Responses retain
the request ID and typed record/revision, add `local_cache.stale:true`,
`remote_confirmed:false`, the snapshot commit, prior verification time and queue
counts, and retain `persistence.durable:false,state:read-only`. Cached data is not
current Server state, GitHub backup confirmation or a merge of pending writes.
Offline catalog describes only the prior snapshot's authorized contents.

Cache reads require matching policy, native binding and service/principal identity,
verified snapshot/owned-file hashes and a completed mapping transaction. The
service identity pin is created only after a successful authenticated remote
refresh. Older unbound caches require one online verification; they are not
silently adopted offline. Authentication/scope/credential errors remain errors. A known service refusal
blocks offline use across restarts until authenticated refresh succeeds, without
deleting downloaded content.
Missing snapshots or absent cached IDs return `content_offline_cache_miss`, never
`record:null,revision:missing`; unverified cache returns
`content_offline_cache_unverified`. Preserve all cache/queue evidence. Cached CAS
revisions remain original bases: reconnect may produce a conflict, which requires
explicit reconciliation. Cache availability never grants a new operation or
reloads already loaded Rule/Skill/model context. No Server endpoint, write/durable
confirmation, outbox format or background scheduler changes.

A blocked device request is retained and never retried by startup or refresh.
An explicitly repeated content_put with the same request_id, exact parameters and
unchanged queue identity sends the original request once through the approved
transport. Only a strictly validated Server receipt marks it confirmed; unknown
results retain the blocked evidence. A returned CAS conflict enters the existing
conflict reconciliation flow. This changes no grant, tool, request or queue schema.

CAS conflicts preserve the submitted request and remote record. Unresolved local
writes or source conflicts block mapping. A new explicit merged content_put may
include local-only `reconciles` metadata selecting exact conflicts of that artifact:
`{"request_ids":["original_operation_id"],"source_keys":[]}`. Source keys are
reported in local import status. This metadata never reaches Server, grants no
permission and cannot alter the original CAS base. The merged request uses a new
ID and the read revision. Evidence remains; only its durable confirmation resolves
the selected conflicts. A disconnected conflict read is retried after reconnect.

The local-only `content_queue_inspect` tool is offered within existing content
write grants. Empty arguments list at most 64 path-free queue metadata entries;
`cursor` continues the same snapshot and fails if it changes. An exact
`request_id` returns the original request, reconciliation references, saved
response and cached remote conflict snapshot. Inspection never drains, fetches,
repairs or maps content. The cached conflict snapshot is not a current CAS base;
read the current target before an explicit merge. No new Server method or queue
schema is introduced.

A confirmed reconciliation revisits earlier dependent requests in the same
drain, retaining their original IDs and CAS bases. Unselected conflicts remain
blocking. An unconfirmed send stops the drain; the same write call's subsequent
mapping refresh does not resend it. Pending outcomes cannot be reconciled as
conflicts: recover the exact original request and ID first. Restart/reconnect
retains the existing exact-request recovery protocol.

MCP `memory_onboard` accepts the bound `harness`, an exact
`destination` (enrolled HTTPS endpoint, actor, and canonical approved origin),
and `source_scope: "shareable-personal-native-memory"`. Set `approved: true`
after the user confirms this host's supported personal native-memory scope is
shareable and approves that exact destination. The Client calculates source
fingerprints locally. It skips Windows CodeBuddy project memories because the
reader cannot verify their origin. An explicitly approved original is submitted
without classifying its text. Native transcripts and databases stay outside the
authorized readers. Original text is limited to 128 KiB of UTF-8 and must fit
the native HTTPS request envelope after JSON encoding.
Installation and future capture approval do not authorize historical transfer.

The Client retains a path-free snapshot of the approved source scope, bound to
the device, host, destination and existing route policy. To resume, call the
same tool with `resume: true`, the same scope and destination, and optionally
the returned `approval_id`. Without an ID, the tool resumes the latest saved
batch at that exact destination. It uses the original snapshot and skips
files added after approval. Source and pause gates are rechecked on resume.
A changed file does not inherit approval for its new fingerprint.

Replies contain the approval reference and aggregate received, already present,
excluded, pending and failed counts without source bodies, paths or credentials.
A verified L0 receipt confirms original acceptance. Extraction is reported
separately. The existing authenticated L0 protocol and durable outbox handle
transfer. Legacy CAS, summary and review entries stay in their existing ledgers.

Other explicit imports accept a reviewed private normalized source plan selected
with --import-plan and --import-plan-sha256. Inventory is persisted
before upload/mapping; it does not scan any additional sources. Stable source IDs
and fingerprints deduplicate repeated execution. Equal remote content is not
uploaded again. Changed sources require a known previous imported base plus Server
CAS; unrecognized remote changes retain both versions for reconciliation. Canonical
forgotten memory markers are conflicts, never vacant first-import targets.

Rule/Skill imports start disabled. Subsequent content imports preserve independently
approved remote enablement. Rule triggers, Skill resources and dependencies remain
typed data; upload is separate from activation. Native ownership checks preserve
hand edits. Coverage reports list read/excluded/unavailable/not-checked sources;
queue zero never proves account-wide memory coverage. Native history/databases,
projects, LocalOnly, secrets, dependency execution and new background jobs remain
outside this batch.

Server code, public Client code and private Data history are separate roles. The
Data origin/branch belongs to Server storage policy, never the Client outbox or
package. A future split must preserve request/CAS/deletion history and separately
review deployment references, credentials and rollback. This batch performs no
repository split or data/history migration.

## HTTPS credentials and persistence

The installed adapter attaches the active device Bearer internally. The original profile and device grant bind read/put and the approved artifact types and IDs. Device credentials are stored through the platform credential boundary. Normal HTTPS requests verify TLS and use the original endpoint. Redirects are rejected.

The Server device gate accepts Content and memory requests and restricts pairing and enrollment to their own paths. It checks device grants, rejects static service and administrator credentials on the device request path, and applies global, enrollment and per-device budgets. Server configuration determines the available capabilities.

Server-local durability and GitHub backup are separate receipts. For Git-backed Content, a strict
local-confirmed write has local_durable=true, github_confirmed=false and
durable=false, with a journaled Server Git commit. Native Memory L1 writes carry
their validated native persistence proof. The device
outbox acknowledges Server storage and continues CAS/mapping without claiming
GitHub persistence. remote-confirmed with durable=true remains the original
GitHub acknowledgement, including legacy receipts without additive flags.
Contradictory flags, missing commit/result or mismatched records stay pending.
The outbox schema and request IDs remain unchanged; confirmed means Server saved
it, with its response describing backup state. Explicit same-ID retry can upgrade
a local receipt without duplicate mutation. No background confirmed-record scan
or scheduler is added. Read/catalog persistence includes Server github_backup;
offline cache cannot establish its current status. Original origin, local HEAD
ownership and CAS remain enforced. Server deployment and compatible Client
publication require separate approval. Missing/unknown write-receipt declarations
retain the original retryable persistence_pending error until GitHub confirms.
0.2.5 and frozen 0.2.6 do not declare this capability. Server deployment need not
wait for their workers to exit; their original GitHub-outage queue behavior stays.
The HTTPS client declares this capability. The transport validates
local-confirmed and remote-confirmed receipts, and the content adapter validates
the returned record. The header leaves authorization, CAS and the request digest
unchanged.

Additive receipt labels distinguish operation and storage: writes may carry
operation=write, save_status=saved and backup_status=pending/confirmed. Reads may
carry operation=read, read_success=true and write_receipt=false; their durable=false
is not a failed write. A pending backup with automatic_retry.state=waiting is a
separate background task, not a failed save. The Server preserves last_error and
retry metadata; attention_required=true and status_note identify paused retries,
review requirements or invalid local ownership. Clients must report those faults
without rewriting, polling or pretending backup is confirmed.

Native refresh authenticates a live catalog before reusing records from the
service/policy-bound verified snapshot. Only matching revisions are reused;
changed records are read again, removed records disappear, and a second catalog
must still match before applying mappings. This bounds ordinary refresh traffic
without expanding scope or accepting a stale snapshot as remote confirmation.
Only changed host files enter the write transaction; unchanged Skills and AGENTS
are checked for ownership but never reopened for write. Snapshot-only changes
report native_changed=false after successfully advancing the verified snapshot.
Refresh failures include a nonsecret stage/reason and retain prior files and
snapshots. A mapping failure does not invalidate an independent Server save receipt.

For daily memory and device maintenance, see the [memory guide](https://github.com/goodname427/agent-brain-client/blob/main/docs/reference/memory-behavior.md) and [maintenance guide](https://github.com/goodname427/agent-brain-client/blob/main/docs/installation/maintenance.md). This protocol defines method schemas, authorization, CAS and receipt semantics. `content_catalog` reports authorized Agent Brain destination contents. `memory_query` accepts native per-namespace L1 count and recall options. L2/L3 are assembled independently.

The current MCP `memory_query` takes a nonempty `query` and optional `origin`, `session_id`, `limit`, and `recall`. Use the installed tool schema for the supported settings. Device and source authorization govern what can be recalled. This read path leaves unrelated blocked writes in the outbox.

## Explicit native event source support

The source supports an explicitly invoked HTTPS `--event-hook` worker with an
exact pinned hook policy, profile and content policy. This does not register a
handler, trust a definition, enable capture, enroll a device or publish a release.
Normal MCP, notify, installation, update and legacy Memory query paths retain
their existing behavior. The internal `memory_event_submit` method is excluded
from MCP tools and the native installation allowlist.

The hook policy selects at most sixteen exact canonical cwd/project/session
bindings and an existing private directory for its off marker. Disabled policies
or any off marker stop processing before stdin and identity activation. Payloads
require a turn ID and either UserPromptSubmit prompt text or a nonrecursive Stop
final message. Text is bounded to 8192 UTF-16 units per message; the worker never
opens a supplied transcript path or scans history. Prompt stages locally; Stop
enqueues the same immutable two-message event before waiting on the content lock.
Only an explicit matching Stop retries that event under the current off gate;
normal refresh does not drain pending events. Event submission uses the existing
HTTPS device credential and endpoint. No extraction or model task is invoked.

An exact `core-accepted` receipt confirms acceptance and its receipt ledger only:
it retains durable=false, github_confirmed=false and model_extraction_verified=false.
Missing, ambiguous or malformed receipts retain the original pending request.
Hook output is protocol-only, exits successfully on capture exceptions and does
not request a model continuation. Exit 0 does not prove capture or staging. Any
real native verification requires a separately authorized current execution window.

## 0.4 bounded device task extension (unpublished source)

Explicit owner tasks may use memory_extract(project_id,session_id) and a scoped
memory_query with context. They require a pinned finite device policy binding the
original profile, actor, device, endpoint and exact request IDs/parameters. A
read-only task has extract_request=null and a two-batch selection; it cannot
extract. These operations are not added to ordinary MCP methods or enrollment
grants. Client and Server both reject changed scope/selection and ambiguous
responses. Extraction accepts only a strict core-extracted receipt with integer
HTTP count and the three succeeded stages; Core/Git durability is not inferred.
Exact recall validates L0 empty, one L1/L2/L3, selection/content digests and
committed source provenance. It does not assert model output truth. No implicit
retry, new credential, background upload, capture registration or model call is
introduced by installing the source.

## Address and long-term access-key bootstrap

The explicitly selected, normally verified HTTPS Server origin supplies bootstrap authority. POST `/v1/access/bootstrap` uses `Authorization: Bearer <access-key>` and body `{"device":{"adapter":"codex","platform":"<actual-platform>"}}`; only successful authentication returns `{"schema":1,"connection":<credential-free-platform-connection>}`. The connection pins the exact grant and official release origin/ref/anchor. Subsequent Git ancestry, manifest, file-scope and health checks remain required; hashes supplied alongside untrusted package bytes cannot establish trust.

POST `/v1/access/enroll` uses the same authorization and device metadata plus a stable nonce, returning the existing pending device credential/nonce/device_id/grant/phase shape. The installer persists the separate device credential in the existing OS store before calling `/v1/pairing/confirm`. Lost replies reuse the nonce. Long-term keys remain in an origin-specific OS item and never authorize content/admin requests directly. Server stores only key digests with a frozen access-policy digest and revocation state; revocation also rejects linked devices. No expiry is imposed by default. Existing invitation enrollment and device protocols remain compatible.

Both bootstrap requests refuse redirects and use normal TLS certificate/hostname verification. They send no key in URL, JSON body, CLI arguments, environment or plaintext state. Explicit access_denied may retire only the rejected origin key; uncertain transport outcomes preserve it. Installation success still requires the owned local transaction and read-only protocol check. Access grants do not approve source directories, hooks, native session access or unlimited collection.

The verified local snapshot loader and refresh path accept collections spanning
multiple catalog pages. Refresh fetches all pages, checks revisions, and repeats
the paged catalog before committing mappings. A page race retains the prior
verified snapshot. Restart and offline inventory retain the complete collection
and expose the same page shape. Snapshot byte budgets remain independent local
resource guards; they do not impose a 256-record count limit.

## Historical transports and candidates

The exact [historical protocol text](https://github.com/goodname427/agent-brain-client/blob/7136ee3319511ccb27df5a03f413d6b583eba1ed/docs/reference/PROTOCOL.md) records the legacy Topic method schemas, the private HTTPS/SSH request path, signed-device and issuer candidates, retained pairing transactions and receipt negotiation for older releases. Use that record to interpret an old binding or receipt. Current device setup and updates start with the official platform installer.
