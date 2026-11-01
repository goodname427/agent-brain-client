# 服务协议 1

认证为获授权 Bearer token，服务器 grant 固定身份与 read/write。只接受 HTTPS 或 SSH 隧道回环 HTTP，重定向不转发凭据。请求 POST /v1/requests，JSON 为 request_id/method/params。request_id 为稳定的 16–128 字符；重试同一写入保持 ID 和完整参数。

topic(id)、recall(query,limit) 只读；remember(id,title,description,body,expected_revision,type)、forget(id,expected_revision) 写入。新主题 revision 为 missing；旧主题必须使用读出的 revision。客户端不能传 Core 路径、actor、凭据、项目或 local_only。写头声明 X-Agent-Brain-Adapter=codex、X-Agent-Brain-Protocol=1、X-Agent-Brain-State-Schema=1；它们是兼容信息，不授予身份或权限。

只有响应 persistence.durable=true、state=remote-confirmed 才说明该请求已确认远端保存。CAS 冲突重读协调，用新请求 ID；网络/503 中断保留旧 ID 重试，不能声称尚未发生写入。client_upgrade_required 说明版本不兼容；storage_unowned 或 operation_uncertain 要保留现场交 Agent 处理，不重置或强推。

client_updates(adapter,platform,installed_version) 只读发现状态、受信 release manifest/commit 和兼容信息。未配置时为 not-configured；配置后为 current、compatible-update、adapter-review-required、release-review-required 或 release-unavailable，并给 next_action。服务目录检查不激活代码；客户端只用已经固定的 policy origin/ref/anchor 和 SHA256 检查同一发布，不把服务返回的来源当授权。桥接由固定 startup 传入逻辑 Harness 平台与已验证版本，不能把执行桥接的 Linux 服务器冒称 Mac Harness。数据提交与客户端发布分开。

stdio MCP 桥接器提供相同五个方法；记忆写工具必须由调用者提供 request_id 和 expected_revision，MCP JSON-RPC id 不充当跨进程幂等 ID。工具 arguments 不能传 token、路径、actor 或作用域。工具执行失败用 isError=true 和原服务 error/durable=false 返回，传输未确认时保留原参数重试。initialize/initialized 与 tools/list/call 只做协议协商；clientInfo 和 _meta 不授予身份。它不宣称 HTTP API 已实现 MCP Streamable HTTP，也不自动安装 native 配置、启动调度或迁移真实数据。

personal模式的每个请求额外声明X-Agent-Brain-Data-Mode=personal，仍要求Codex/API1/schema1；不合约调用在数据读写前拒绝。固定personal policy绑定独立root/origin/branch/actor及导入manifest SHA。import-only只接受清单内稳定request_id与完整请求摘要一致的remember，expected_revision必须missing；explicit-personal才允许其他已授权个人写操作。固定清单导入始终不覆盖已有主题，失败保留同一清单、请求ID与服务回执恢复。策略、身份、真实内容采集范围不会由请求参数授予。

## Unified content v1 candidate (opt-in; not a stable permission grant)

The private service candidate supports `content_catalog`, `content_read`,
`content_put`, and `content_remove` at the existing versioned request boundary.
These methods are disabled by default and are not added to the current five MCP
tools. Existing token grants cover memory only. A paired device needs explicit
read/write scopes, `artifact_types`, and frozen `ids` keyed by every granted type.
Only memory may explicitly use `"*"`; Rule/Skill IDs are exact lists. The signed
proxy rejects unlisted reads and CAS writes before accessing credentials; the
Server independently enforces these IDs and filters catalog metadata. Legacy
memory methods are denied for devices whose memory grant uses an ID list.
Catalog cannot request extra types. The
existing personal policy rejects these methods without a separately pinned
content policy. That opt-in policy binds the same store, actor, prior policy SHA,
exact IDs, rule modes and dependencies. Memory keeps import-only/daily-personal
write restrictions, and paired content devices cannot bypass it via legacy data
methods. Activation and native tool permissions require their deployment approval.

All requests retain the stable `request_id`; writes retain
`expected_revision` (`missing` or SHA256). Params are exact:

| Method | Params |
| --- | --- |
| `content_catalog` | `kinds`: nonempty unique list of supported kinds |
| `content_read` | `kind`, `id` |
| `content_put` | `kind`, `id`, `record`, `expected_revision` |
| `content_remove` | `kind`, `id`, `expected_revision` |

Default personal scope is `read/put`, exposing catalog/read/put only. `put` permits both creation and CAS update. Legacy `write` is a put alias for typed content and does not imply removal. `content_remove` requires a separately approved explicit `remove` scope at both device and Server policy boundaries; personal daily Memory removal remains blocked. Protocol support never grants an operation automatically. The unchanged signed component candidate supports its reviewed read/write profile and rejects removal; no new signing permission is inferred.

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


The optional operator-only `/v1/pairing/issue` route is disabled by default. Its
private issuer policy binds an independent administrator credential hash, fixed
grant/TTL, personal/content policy hashes and data store. Clients send only the
pinned issuer-policy digest and device adapter/platform, never new grant fields.
Legacy and paired credentials cannot act as issuer credentials. The signed
component consumes the short-lived code internally; it returns only pairing
metadata. No issuance or administrator credential operation is an MCP tool.


The separate ssh-server-credential-proxy capability carries only typed JSON on
a strictly pinned SSH stdio channel. The fixed server process owns the bearer
record; the Mac owns no API/operator secret. It rejects model-selected paths,
authentication, legacy memory methods and grant changes. Fixed read-only
client_updates discovery is also allowed; its source metadata grants no trust. Credentials are provisioned
under an explicit server-only plan, saved pending before confirm, and never
returned by that command. Policy/record digests and the frozen grant are checked
before each forwarded request. Reads/puts/removes still cross the same Server
HTTP boundary and journal/CAS/confirmed-Git rules. The SSH identity and forced
command association must be explicitly approved; an existing five-tool MCP
command does not grant this candidate protocol. Only memory supports '*'.

Codex content writes are saved in a private local outbox before any transport.
The complete typed parameters, original request ID and CAS revision are atomically
written and the file/directories fsynced. Startup and write boundaries replay those
exact requests after reconnect or restart. Client 0.2.5 requires a matching
request ID, durable=true, state=remote-confirmed, a Git commit and matching content.
Only a client declaring X-Agent-Brain-Write-Receipt: local-durable-v1 accepts
the strict local-confirmed receipt described below; frozen 0.2.6 does not declare it.
confirmed then means Server storage, with GitHub backup reported separately.
Memory follows Core's boundary-whitespace/CRLF normalization and opaque metadata
revision; Rule/Skill revisions must match the returned JSON record. Queued means
saved on the device, awaiting a validated Server storage receipt. Lost acknowledgements
never generate a new operation ID or silently change expected_revision.

Queue identity binds the native mapping policy and service/principal/destination,
independently of the Core code repository or private Data remote. Switching the
service identity blocks replay until pending operations are reviewed. No API
credential or Git repository URL is stored in the outbox. Queue/import state is
bounded to 1024 entries and 1 MiB per file; capacity failures preserve prior state
for review. Confirmed entries remain available as evidence. Private temporary
files left by killed atomic saves are retained; only renamed request files replay.

Client 0.2.1 introduces local outbox/import state. Release transactions never
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
Catalogs retain their complete authorized inventory and existing 256-entry bound;
typed records, revisions and scope checks remain required. Mapping refresh and
outbox/import recovery still run at MCP startup and after confirmed writes.
Read responses report `local_refresh.status:read-only` and do not claim model reload.

Full inventory uses `content_catalog` with `kinds` only. The optional Memory-only
query/since/until/limit alias is bounded retrieval (limit 1..3), not pagination or
full Onboard coverage. Invalid parameters are local JSON-RPC `-32602`, with
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

First/import-again handling accepts an explicit reviewed private normalized source
plan selected with --import-plan and --import-plan-sha256. Inventory is persisted
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

This alternative does not relax the signed-device-proxy requirement. Each
transport has an exact independent file/runtime/capability policy; signed
native changes still require the separately pinned Swift/binary/identity plan.

A replacement from the owned legacy MCP may explicitly include retirement of
its AGENTS.md block as a third native path. The old complete receipt must prove
the exact preimage; otherwise preserve the file. Install rollback restores all
planned paths. Later cloud mapping is independently owned by the bound private
content cache; legitimate mapped hashes are accepted, external edits are blocked.

The opt-in HTTPS system-credential candidate uses the same typed contract and
durable outbox. Its trusted adapter adds an active device Bearer internally;
credentials are never tool arguments/results. The profile freezes read/put and either the legacy 31 exact IDs or the explicit
personal-owner collection described in docs/PERSONAL-OWNER-SCOPE.md. No plaintext fallback, redirect, ambient proxy, SSH client identity,
Apple Team or access-group is required by this candidate.

Native-only pairing saves preparing(code, nonce, profile digest) before POST
/v1/pairing/prepare, then pending(credential, nonce, device ID, exact grant) before
POST /v1/pairing/confirm. Active confirm remains replayable after invite expiry.
Failed saves prevent the next network step; cancellation creates no item. Unknown
or changed records are preserved. Terminal reset rechecks Server and requires a
native confirmation UI plus exact observed-record CAS.

The separately enabled Server gate accepts only three internal POST paths:
/v1/device/requests, /v1/device/pairing/prepare, /v1/device/pairing/confirm. It rejects
static service/admin tokens and legacy methods, enforces the grant before pending
or activation, and applies bounded global/pairing/per-device budgets. Responses
are no-store; fixed errors omit raw secrets. Original private routes remain.
Public routing, issuer/key/ledger configuration and real credential installation
require separate precise deployment approval; exporting source enables no route.

Server-local durability and GitHub backup are separate receipts. A strict
local-confirmed write has local_durable=true, github_confirmed=false and
durable=false; its commit is a journaled, hardened Server Git commit. The device
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
The new HTTPS client declares the capability and its HTTP transport accepts only
strict local-confirmed receipts before the content adapter validates the record.
The header changes neither authorization, CAS nor the request digest.

Memory behavior and source-coverage decisions belong to the currently loaded `agent-brain-memory` and `agent-brain-onboard` Skills. This protocol defines content method schemas, field and authorization constraints, CAS and receipt semantics only; it does not set daily memory candidate thresholds or onboarding/source-import policy. If a relevant Skill is unavailable, report that gap. `content_catalog` reports AgentBrain destination contents, not Harness-native source inventory. `memory_query` limits each retrieval to at most three results; that query bound does not define source-review or import coverage.

memory_query is an additive read-only typed method: kind=memory, query string (empty for recent), since/until nullable offset-aware ISO8601, limit 1..3. It returns current authorized topic previews/revisions and server memory updated_at, never event occurrence time or historical erased preferences. Existing content_catalog may carry Memory-only query options at the MCP layer and delegates to this method, retaining the original installed tool whitelist; normal catalog calls and v1 record/envelope shapes remain unchanged. Query does not refresh/drain unrelated blocked writes; unavailable/offline query stays an error. No new credential/scope/SQL migration, background collector or raw-session access.

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

## Address and long-term access-key bootstrap (installer 0.5.1)

The explicitly selected, normally verified HTTPS Server origin supplies bootstrap authority. POST `/v1/access/bootstrap` uses `Authorization: Bearer <access-key>` and body `{"device":{"adapter":"codex","platform":"<actual-platform>"}}`; only successful authentication returns `{"schema":1,"connection":<credential-free-platform-connection>}`. The connection pins the exact grant and official release origin/ref/anchor. Subsequent Git ancestry, manifest, file-scope and health checks remain required; hashes supplied alongside untrusted package bytes cannot establish trust.

POST `/v1/access/enroll` uses the same authorization and device metadata plus a stable nonce, returning the existing pending device credential/nonce/device_id/grant/phase shape. The installer persists the separate device credential in the existing OS store before calling `/v1/pairing/confirm`. Lost replies reuse the nonce. Long-term keys remain in an origin-specific OS item and never authorize content/admin requests directly. Server stores only key digests with a frozen access-policy digest and revocation state; revocation also rejects linked devices. No expiry is imposed by default. Existing invitation enrollment and device protocols remain compatible.

Both bootstrap requests refuse redirects and use normal TLS certificate/hostname verification. They send no key in URL, JSON body, CLI arguments, environment or plaintext state. Explicit access_denied may retire only the rejected origin key; uncertain transport outcomes preserve it. Installation success still requires the owned local transaction and read-only protocol check. Access grants do not approve source directories, hooks, native session access or unlimited collection.
