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
written and the file/directories fsynced. Startup and tool boundaries replay those
exact requests after reconnect or restart. Completion requires the matching Server
request ID, durable=true, state=remote-confirmed, a Git commit and matching content.
Memory follows Core's boundary-whitespace/CRLF normalization and opaque metadata
revision; Rule/Skill revisions must match the returned JSON record. Queued means
saved locally, awaiting durable Server/GitHub confirmation. Lost acknowledgements
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
