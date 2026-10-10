# 自开发适配器约束

先查实际 Harness 的官方加载入口、权限、规则/Skill 发现与启动/会话接口。不要根据目录存在或模型名称宣称已加载；缺文档或执行接口时保留实验状态，报告具体未完成项。

薄适配器负责请求转换、获授权 token、生命周期和 Agent 可见状态；不复制 Core、数据 Git、记忆索引、主题格式或事务逻辑。遵守 PROTOCOL.md 的 revision、幂等和持久确认；不接受客户端任意路径或 actor 扩权，不 quiet 吞掉阻碍。使用原生记忆和工具的正常接口，不接管数据库或替换原生工作流。

正式客户端包应使用既有 release manifest/ReleaseEngine 协议，限制文件范围、Core API/state schema、平台、依赖和能力；兼容更新使用同一校验、快照、健康和回滚路径，本地修改不覆盖。能力或迁移变化必须给出 Agent 可执行的受审步骤。服务端目前只接 Codex，新 Harness 不因本文存在就获支持或新权限。

## 旧版迁移与可恢复清理

本流程供收到本仓库链接的实际 Agent 执行。设备/Harness 名称以当前工具接口和配置为准；不能根据模型名或目录存在声称支持。本文使用 `<旧仓库>`、`<设备home>`、`<已批准profile>`、`<迁移回执>` 等占位符，不提供真实身份、地址或私有部署资料。

已实测的是 Mac Codex：已有受限 SSH stdio 身份、服务器固定 bootstrap 与受信发布政策，旧本地 Brain hooks 和个人写 Skill 入口暂停后，新 MCP 完成配置及协议检查。合成测试覆盖兼容更新、离线保留旧包、失败恢复及 CAS/幂等；获授权的个人模式另有服务端门禁。没有执行新模型或 GUI 会话验收。其他 Harness、Windows、Linux 客户端以及不同认证方式未完成这套设备迁移实测；按本文件前文开发适配器，报告证据缺口，不套用 Mac 路径、系统废纸篓或猜测命令。Linux 服务端运行不等于 Linux 客户端支持。

### 1. 识别与保留现场

先运行 `python3 connect.py --agent codex`（运行环境需 Python 3.11+）。读取返回的接入状态和必要的现有登记：

| 状态 | 下一步 |
| --- | --- |
| `native-client-registered` | 核对自有配置或完整迁移回执，复用现有入口，继续依赖核对。 |
| `migration-review-required` | 保留旧绑定；准备精确迁移计划及备份。当前命令不会自动停旧 hooks 或迁移数据。 |
| `server-authorization-required` | 缺少已批准服务 profile；说明具体缺口，不生成身份或索取聊天中的秘密。 |
| `native-repair-review-required` / `local-state-review-required` | 保留未知、漂移、链接或被 override 遮蔽的配置，先核对所有权。 |
| `service-change-review-required` / `adapter-review-required` | 明确列出身份、固定命令、模式、目标或实际 Harness 的变化；不能靠更新包授予新权限。 |

检查 `<旧仓库>` 的分支、worktree、未提交/未推送改动，以及正在使用旧入口的进程和任务。只读取任务所需配置、登记和项目路径映射；不读取原生 session 文件、历史数据库或助手自定义审批规则。不要终止不明进程、重置工作区或用目录名判断可删。

列出新入口直接和间接引用的文件：MCP 命令、SSH 身份/known_hosts、固定远端命令、bootstrap/受信政策、客户端登记、完整迁移回执与备份。追踪仍用旧 Core 的其他项目/设备和 Skill 链接。仓库若同时承载这些文件或当前开发 worktree，就不是整体清理对象。

只读盘点尚未迁移的记忆、LocalOnly/私有库、项目资料、Rules 和 Skills；记录文件清单/哈希、规则启用状态、Skill 运行依赖及引用，不公开正文。Rules/Skills 的盘点不等于迁移授权；尤其不能读取或复制助手自定义审批规则。含凭据的文件只核对必要路径/权限/引用，不读值。列出唯一数据及未同步改动，避免把生成适配器与其承载的数据混同。

### 2. 迁移前条件与现有步骤

迁移计划需具体到自有托管块、一个 MCP 表、旧 hooks/Skill 链接、备份及前后哈希。迁移范围、已有身份、数据模式和服务目标必须在当前用户授权内；已授权的步骤继续执行，不反复索取确认。缺授权或执行权限时才说明精确缺口。当前公开客户端没有通用的旧数据迁移/一键清空命令。

Mac Codex 的现有检查命令：

```sh
python3 connect.py --agent codex --service-profile <已批准profile>
```

profile 需本机私密权限、准确平台、现有受限身份、固定远端命令及严格主机校验。无旧重叠绑定且返回 `ready-to-register` 时，在已有授权内使用刚计算的摘要：

```sh
python3 connect.py --agent codex --service-profile <已批准profile> --apply --plan-sha256 <预检摘要>
```

若返回 `migration-review-required`，此 apply 不会绕过迁移：Agent 按具体已批准计划备份自有文件/链接，停止同范围旧写入口，注册新入口并形成完整回执。保留其他原生设置、规则、模型/速度策略、项目和设备登记；已停用的 hooks/任务无需再次修改安全设置。不同时运行两套个人写入口。真实数据导入只由另外获授权的后台清单和工具完成，公开客户端不会复制私库 Git 历史、直接运行数据 Git 或自动采集会话。

### 3. 验证新客户端

再次运行 `python3 connect.py --agent codex`：自有配置/迁移回执哈希一致，初始化、五个工具及只读 `client_updates` 协议检查通过即可报告**安装成功，配置及协议检查通过**。这不声称模型已经加载指令；不把用户新开会话、付费模型或 GUI 验收列为必做条件。

在固定 bootstrap 已安装的机器和现有执行授权内，可使用原有更新检查命令：

```sh
python3 <固定bootstrap>/adapters/codex/client_startup.py --policy-file <受信policy> --policy-sha256 <固定摘要> --check-only
```

检查源/ref/anchor、版本、文件哈希、平台、能力、协议及依赖。`not-configured` 表示尚无正式受信源；`release-review-required` 表示兼容/政策阻碍；网络暂不可用时报告状态并保留已验证包，不覆盖本地改动或扩大源信任。`client_updates` 是发现信息，不能代替固定政策授权。个人模式还需核对服务器指导与本机托管块一致；只用合成数据测试写入，生产验收优先只读，不能为验收编造用户记忆。

### 4. 可恢复清理与保留清单

确认新客户端不再依赖某个旧对象、没有活动使用者、未提交改动或唯一数据且迁移完整后，才按已有清理授权移到系统废纸篓或其他已核验的可恢复位置。移动的是精确对象；不永久删除、不清空废纸篓，不移动当前开发工作区、远端仓库或仍有其他角色的旧仓库。生成缓存和确认被替代的旧适配器代码可分别处理；先记录源、目的地、内容哈希、权限及恢复位置。

保留清单必须逐项给出原因：未迁移 Rules/Skills/私有或项目资料、未推送改动、当前开发 worktree、新客户端引用的身份/主机校验/回执、其他项目/设备仍使用的入口，以及备份/历史核验资料。没有安全可清理对象时如实报告“保留”及依赖，不为满足“清空”而损坏它们。

清理后复核配置及完整回执、实际 SSH MCP 协议和版本/模式；确认数据未变化、旧写入口没有恢复、其他设置及登记哈希保持一致。报告实际移动清单、保留清单、剩余依赖及复核结果。

### 5. 失败与用户介入

客户端包兼容更新复用 ReleaseEngine：健康失败或中断时恢复旧包指针；离线继续已验证包。它不恢复原生配置或记忆数据。设备迁移失败则保留现场、回执和备份；仅在原授权、所有权及前后哈希仍匹配时恢复精确自有文件/链接。清理恢复从记录的可恢复位置放回原路径；目标已有新内容时保留双方并报告冲突，不覆盖。

需要用户介入的具体条件是：缺少服务/身份或真实数据迁移授权；需要新凭据、持久权限或不同平台接口；某项唯一数据的去向无法确定；外部改动导致恢复/迁移冲突；或执行权限/自动审批拒绝。明确说出对象、缺口和来源，完成不受阻的已授权工作。不能将暂时网络失败、额外模型行为测试或已经给过的授权重复变成用户必做步骤。

## 安全配对候选：尚未部署

配对是另行准备的源代码候选，当前 stable 包和现有 SSH MCP 不依赖它。服务端默认关闭配对入口，没有凭据签发的模型工具、隐式 Keychain 操作或新监听。第一段验证使用合成码、合成范围和内存存储；Swift 组件仅编译及离线 `--self-check`，没有真实签名安装或 Keychain 运行验收。

候选包提供 `adapters/codex/client_pairing.py` 和 `native/DeviceComponent.swift`。生成候选不会复制 Git 历史、Core、服务权限、用户资料、签名材料或本机 profile；它没有 stable release manifest，既有 13 个 payload 文件的发布政策会拒绝其新增文件，需另外审核版本/能力/签名与范围才能成为兼容发布源。

Agent 只读检查 `<设备home>/.config/agent-brain-client/pairing-plan.json` 或使用 `connect.py --agent codex --pairing-plan <计划>`。计划固定 schema、实际 Mac 平台、组件与私密 profile 的绝对路径及 SHA256。profile 固定 HTTPS 服务目标、Keychain access group 和被冻结的设备权限范围。发现计划不能代替签名、传输和权限批准；`pairing-ready-for-secure-user-input` 仅表示元数据相符，不能报告已配对或安装。现有原生/HTTP 登记和旧 Brain 迁移检查优先，不能用配对计划覆盖它们。未知 Harness 仍返回 `adapter-review-required`，待开发并验证适配器。

真实配对由已批准、正确签名的设备组件显示原生安全输入框；配对码不进入聊天、工具参数、环境变量或命令行。服务端使用短期高熵一次性码绑定 actor、读写范围、内容类型、适配器和平台，prepare 返回的凭据先由组件原子保存到指定 Data Protection Keychain access group，再 confirm 激活。确认响应丢失可从原记录恢复；过期、撤销、权限或 profile 漂移需要报告具体阻碍，不能删除未知 Keychain 项或放宽权限来强制重配。普通 content 请求可由组件代理候选使用同一 Keychain 记录；Python 侧只检查冻结 profile/组件哈希、签名 identifier/Team ID 与 access group，传递非凭据请求并接收服务响应，不查询或导出凭据。已有安全输入与原生安装候选代码；真实签名/Keychain、管理员发放和生产运行未验收，不能声称正式安装闭环。系统安全存储不能承诺对同用户下所有进程绝对隔离。

HTTPS 只是当前候选传输；协调接口不绑定具体部署形式，可由后续已批准的 HTTPS、受限 SSH 或 Secure MCP Tunnel 实现。此候选没有部署隧道、OAuth、ChatGPT 或其他 Harness。后续普通工具请求仍经 Adapter → Server，GitHub 只由 Server 持久化及溯源。Memory/Rule/Skill 等内容的本地版本缓存和原生映射后续另行验证；缓存刷新不等于替换已加载模型上下文。

## 统一内容缓存／原生映射候选：尚未接入真实 Harness

配对确认明确返回 `pairing_expired` 或 `device_revoked` 时，候选返回 `pairing-reenrollment-review-required`，保留当前记录。已批准的组件可用 `--reset-enrollment --profile <私密profile> --profile-sha256 <固定摘要>`：它先确认服务端终止状态，再显示原生确认框，仅清除与刚读取记录一致的持久引用；当前有效、未知、漂移或并发替换的记录均保留。成功后才提示重新获取短期码并走安全输入流程。Agent 不能用模型工具静默清除或自动确认该 UI。本轮仅合成检查及编译，没有真实 Keychain 删除。

独立 content candidate 包含上述配对候选，再增加中立的 content contract、`content_cache.py`、`content_mcp.py` 和 `content_install.py`，共 19 个 payload 加导出 manifest。可在私有／合成源准备 `--data-mode personal-content --release-version <版本>` 清单；这不发布或自动批准源。实际已部署稳定包仍为原有 13 个 payload。

适配器的显式政策固定 Codex、内容协议、类型、Rule/Skill ID 和允许依赖；Memory 可在批准的个人范围使用 ID 通配。传输由调用方注入，不查找凭据或私库。读取目录、各条正文和再次目录检查得到同一已确认版本后，才准备本地映射；云端发生并发变化时保留原快照。Memory 是可重建文本缓存，Rule 保留启用与 always/on-demand/conditional 触发语义，Skill 只向自有 `.agents/skills/<id>/` 映射文本包。未验证 Python 模块依赖明确阻碍同步；不导入模块、安装依赖、执行 Skill 脚本或读取原生会话。

原生映射只管理实际 Codex home 的一个独立 `AGENTS.md` 托管块和登记文件；明确的 `--codex-home` 优先，其次 `CODEX_HOME`，默认 `<device-home>/.codex`。原生路径必须明确且绝对；Skills 保持 `<device-home>/.agents/skills`。私密回执固定这两个目录绑定，后续变化先阻断而不把成功写入错误目录当成刷新。首次遇到 Skill 文件重叠、已托管文件或政策漂移、链接或未知状态时保留现场。快照、活动指针、原生文件前后哈希与备份在私密缓存中；失败恢复自有字节及权限，中断在下次启动先恢复。恢复时发现外部修改则不覆盖双方，返回 `content-recovery-review-required` 和不确定的 native_changed，不能声称全部回滚。启用 Skill 必须有唯一非空 description frontmatter，与共享记录一致；未知复杂格式明确阻碍发现验收。

设备只选部分 Rule/Skill ID 时，刷新保留完整目录做版本竞争检查，但只取回并映射获准 ID；范围外记录不会阻碍合法内容刷新。目录工具也只返回获准 ID，不能通过 tools/list/call 枚举范围外名字。类型和写工具广告受冻结设备范围限制。

具体 ID 同时绑定到配对 grant 和摘要固定的签名组件 profile；仅 Memory 可明确使用通配范围。组件在读取凭据前拒绝范围外 ID，Server 独立执行同一门禁并过滤目录。内容政策只能进一步收窄。托管 Skill 整个目录内新增的非自有文件或链接会阻断当前／更新／首次认领流程，保留外部文件；Skill 名称在服务端写入前即须与 ID 一致。

接线后的启动和工具调用前后会自动检查云端变化；当前独立四工具 MCP 候选已用假 Codex home、合成服务验证初始化、工具目录、CAS 写入、持久确认与原生映射。它只列出冻结设备范围允许的工具，源代码 CLI 要求固定摘要的设备/签名/内容政策，不发现秘密。真实签名组件、Keychain、HTTPS 及原生安装尚未接线；现有五工具 MCP 不变。它没有空闲时轮询或新后台进程，不能宣称“无调用时实时同步”。`content-refreshed` 表示文件映射完成；`model_loaded:false` 明确表示没有验证已运行模型重新读取上下文。原生映射与已加载上下文是不同验收项。


签名组件候选 profile 还必须摘要固定 `data_mode: synthetic|personal`。personal 模式的内容请求发送完整的 X-Agent-Brain-Data-Mode=personal，未知或缺失模式阻断；它不由模型请求动态选择。新增字段改变 profile 摘要及 Keychain namespace，旧未安装候选不会自动转换或清理已有项，实际变更仍按精确安装计划审批。Swift 离线自检验证声明；真实签名、Keychain 和 HTTPS 尚未执行。


## 内容客户端安装／发布候选

`content_install.py plan` 以固定 release policy、包哈希、设备/签名/内容政策摘要及实际 home/CODEX_HOME 准备私密计划。准备只读；首次认领不覆盖未知 MCP/登记，替换已有自有 MCP 必须明确 `--replace-existing` 且核对当前登记和表哈希。`apply` 要求计划摘要及该真实操作授权，只改同名 MCP 表和登记文件；替换有可验证旧迁移回执的指令块时，计划明确增加 AGENTS.md 第三文件，精确退役旧块，其他正文保留并共同恢复。默认工具批准仍为 writes；其他模型、安全设置、MCP、hooks、Skills 不被安装事务改动。失败按完整前后像预检恢复；外部改动／链接保留双方，返回 recovery-review-required，中断在下次先恢复。

安装前后运行固定 `--installation-check`：MCP initialize、tools/list 与只读 catalog 认证，不建立内容缓存、不写 AGENTS/Rules/Skills，不运行模型。配置和协议检查成功即可报告 installed=true、model_loaded=false，不要求用户新开会话。正常 MCP 启动及工具边界才按另获授权的内容政策刷新映射。`connect.py --agent codex --content-install-plan <私密计划> --plan-sha256 <摘要>` 默认检查；获授权 apply 加 `--apply`。已有 content 登记由普通单链接 connect 识别并核对自有原生表、发布政策、代码哈希及已验证活动版本。

内容发布政策显式固定 capabilities=[codex-stdio-mcp,personal-content-explicit,signed-device-proxy]、19 文件范围和私密 runtime；旧 13 文件能力／政策拒绝新范围。runtime 包含 device_plan/device_plan_sha256、signing_policy/signing_policy_sha256、content_policy/content_policy_sha256、component_source_sha256、device_home、codex_home、content_cache。启动复用既有 GitReleaseSource、ReleaseEngine 与完整性／兼容／健康／恢复流程；catalog 发现只准备包，启动才激活验证版本并运行 content_mcp。源变化、能力／文件／协议不兼容、漂移或健康失败保留／恢复已验证版本，离线也继续原版本。signed component/profile 仍是单独批准的本机资源，不由源码发布自动签名、安装或改 Keychain 权限。

安全签发的候选 profile 可额外固定 issuer_policy_sha256。`--operator-pair --profile <私密profile> --profile-sha256 <摘要>` 使用 NSSecureTextField 原生界面输入单独获批的管理员授权，管理员凭据不保存；短期配对码只在签名组件与 Server 间传递，组件内部直接执行 prepare/save/confirm，不复制到聊天、stdout、argv 或环境。已有设备记录先恢复，不再次签发或要求管理员输入。服务器必须另行显式启用固定 issuer policy，旧 token、设备凭据及模型工具不能签发。本轮只编译／合成验收此界面，实际管理员凭据发放、签名、Keychain 与传输均未执行。


component_source_sha256 固定已批准的签名组件源码关联。候选源码包中的 Swift hash 改变时不会自动激活新包，返回 release-review-required 并保留旧验证版本；须先准备与实际签名二进制关联的具体计划和审核后政策。普通启动还检查安装回执 complete 与自有原生状态；未完成／回滚／漂移时不启动内容映射。安装检查是另一个明确只读入口，可用于安装事务前后，不能冒充已安装状态。


## 独立 SSH 凭据路径与明确安全目标

现存全局 legacy device.json 为其他绑定保留。SSH 内容安装只有 runtime 同时固定已审阅的 legacy_binding_sha256 与 legacy_hooks_sha256，且当前登记及已经暂停的 hooks 前像均匹配，才允许保留它们继续准备；安装事务不写这两个文件。暂停事实需另有已验证回执，摘要不能自行证明 hooks 已暂停。缺失、漂移或未审阅仍阻断；这些摘要仅供安装门禁，不传给 MCP。HTTP 内容登记继续要求专门迁移计划，签名候选的门禁不变。

`ssh_content.py` 只调用固定 `/usr/bin/ssh -F /dev/null -T` 和已批准私密 profile。它不读取私钥原值；OpenSSH 使用现有 identity。固定 host/user/port/known-host hash、固定 sudo 服务账户命令、冻结类型与 IDs；模型不能传 endpoint、token、命令、actor 或任意文件参数。请求只能是四个 typed content 方法及固定 Mac 平台/version 的只读 client_updates 发现，超限、漂移、越权先拒绝；写入仍需 Server 的 durable=true。代理只允许回环 8765，Server 凭据记录的 SHA/范围/账号必须与独立固定 policy 匹配；拒绝旧操作、签发请求、凭据响应和重定向。

这一路径使模型接口不接触 API 原值，且 Mac 不存 API 凭据。它不承诺阻止同 OS 用户任意进程使用现有 SSH 身份或服务账户读取其私密文件。普通系统 Keychain 也可由原生程序负责 I/O、不向模型输出原值，不因此必需付费 Team；但现有代码没有普通 Keychain 实现，不冒称已完成。独立 team-keychain 候选继续保留原 codesign/Team/access-group 门禁，不用 ad-hoc 冒充它；其 entitlement dry-run 只检查摘要固定的二进制与签名声明，不能证明真实 Keychain 授权。

`brain_activation_plan.py` 在私有 Core 中生成分阶段参数计划；`brain_credential_delivery.py` 只在服务账户、私密预建目录和精确部署计划 SHA 下生成/复用 authority key并内部交付设备记录。不设置管理员 issuer，不需要把原值发给用户／Mac。文件不覆盖未知记录，确认响应丢失从 pending 恢复；缺失/漂移或不可确认返回 review-required，保留状态，不自动重配。未知客户端仍 adapter-review-required。

`brain_content_migration.py` 仅根据已批准 repository inventory 的名称、触发、依赖、资源和哈希准备迁移提案，不读正文、不上传、不执行 Skill。每个 Rule 保留 enabled/trigger，Skill 源启用状态未知时默认 disabled；已有本地 Core CLI/相对 wrapper 的 Brain Skills 须先改为服务流程，Core 仓库维护保留 repository scope。原生 Skill 目录冲突、UE 编辑器 prerequisites、正文转换/request SHA 与真实目标 revision 尚须具体处理；清单非空不表示迁移完成。合成验证用同样的 IDs/资源结构和改写后的合成正文，不能替代真实 Skill 适配。
