# Mac arm64：安装、使用、卸载、再次安装

支持 Apple Silicon Mac、Codex 和既有 HTTPS 个人 Memory/Rule/Skill 服务。普通客户端无管理 SSH、sudo、发码或 Server/Data 凭据。Windows 保留配置/协议边界，目前返回 unsupported，不支持实装。其他 Harness 与 Intel Mac 不在本入口范围。

## 取得源码和连接文件

在终端执行；缺少系统命令行工具时先完成 macOS 提供的标准工具安装，再重跑。每次开始安装或再次安装都从固定官方 `main` 建立独立的新 checkout，写入本次 commit SHA，然后从该 checkout 启动入口；旧 checkout 和其中的用户修改均保留：

```sh
set -eu
CHECKOUT_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/agent-brain-client-main.XXXXXX")
git clone --depth 1 --single-branch --branch main --no-tags \
  https://github.com/goodname427/agent-brain-client.git "$CHECKOUT_ROOT"
SOURCE_SHA=$(git -C "$CHECKOUT_ROOT" rev-parse HEAD)
printf '%s\n' "$SOURCE_SHA" > "$CHECKOUT_ROOT/.git/agent-brain-source-sha"
printf '%s\n' "$SOURCE_SHA" > "$CHECKOUT_ROOT/.git/agent-brain-bootstrap-once"
printf 'Installer source commit: %s\nCheckout: %s\n' "$SOURCE_SHA" "$CHECKOUT_ROOT"
sh "$CHECKOUT_ROOT/tools/install-mac.sh"
```

下载失败会停止，不会改用旧目录运行。新入口会核对 `origin`、`main`、commit SHA 和干净工作区，再消费一次性标记并执行该 checkout；从普通旧 checkout 启动时才会另建最新 `main` checkout。Git checkout 只负责安装器源码，不改变已安装 MCP 的 HTTPS 发布 origin/ref/anchor 或运行时信任锚。

如果手上只有修改前的旧 checkout，它的 shell 脚本不能被远端 `main` 反向更新。不要从那个目录执行旧入口；先用上述完整命令取得并启动新 checkout。进入新入口后，中断恢复/再次安装继续使用原来的身份、连接、安装计划和审核流程；只有新设备需要发行者交付连接描述。

新设备的连接交付是一个明确的两阶段流程：

1. 在该服务原有、已验证的发行交付渠道申请 **Mac arm64 / Codex 连接资料包**，资料包须包含 `connection.json`、该文件的 SHA-256、发行者身份及准备回执的交回渠道。发行者使用独立 Server 的 [连接交付工具与操作步骤](https://github.com/goodname427/agent-brain-server/blob/main/docs/CONNECTION-DELIVERY.md)，从已有批准的服务范围和发布元数据导出，不发邀请码、不建立设备授权。不要索取开发目录或管理员配置。
2. 通过该渠道核对发行者、文件及摘要，再运行下方安装命令。脚本完成准备后才产生 `ready.json`；按资料包标明的渠道交回该准备回执及摘要，发行者按既有私有邀请控制台审核并安全交付短期邀请，客户端仅在现有安全输入窗口输入。

此仓库没有内置统一公共服务地址、发行者账号或自助发码网站。尚未获得具体服务的可信交付渠道或发行者未配置交付时，停在 `first-install-connection-required`，由该服务发行者补齐资料包和交付入口；示例、任意网站或 Git 当前 HEAD 不能替代。私有 Server 文档可能需要发行者的仓库权限，普通设备不需要取得该权限。

`connection.json` 仅含 HTTPS endpoint、grant（actor、adapter、platform、scopes、artifact_types、ids）与既有官方 release origin/ref/anchor。格式见 [connection.example.json](connection.example.json)：示例为虚构服务，不能用于真实安装；实际 anchor 必须是已批准的发布祖先，不是随意填版本 commit。连接文件没有邀请码、token、nonce、SSH 或 admin 字段，也不是设备凭证，但服务地址和用户范围仍应按私有交付资料保管。`connect.json` 是同类文件的另一种命名，脚本只检查 `--connection` 指定的内容，不依赖文件名。不要把真实部署配置提交到仓库。保持文件和路径稳定；以下变量均为非凭证资料：

```sh
CONNECTION_JSON="/absolute/path/to/connection.json"
CONNECTION_SHA256="<发行者通过可信渠道交付的文件 SHA-256>"
```

## 安装

```sh
sh "$CHECKOUT_ROOT/tools/install-mac.sh" --connection "$CONNECTION_JSON" --expected-connection-sha256 "$CONNECTION_SHA256"
```

终端展示服务器、actor、内容类型、IDs、read/put 范围及发布源；输入 `Continue` 确认，`Cancel` 退出。缺少命令行工具时，脚本请求标准安装并退出，完成后重跑同一命令。它准备 Git HTTPS、Python 3.11+、系统 TLS 根及原有 Swift helper：工具链 Python 不足时获取固定 SHA 校验的 Python 归档，准备到用户私有目录。无需 Apple 分发身份、公证、sudo、远程脚本或更改系统信任。

准备检查成功后提示 `ready.json` 的完整路径。此时管理员才按这份回执发短期邀请；客户端不发码。收到邀请后选择 `Continue`，仅在原有安全输入窗口中输入一次。不要将邀请粘贴到终端、聊天、命令参数、环境变量或剪贴板。取消/Back 可重跑；网络失败、丢 ACK 或重启必须重跑同一命令，继续原配对尝试，不申请新邀请。只有服务端明确拒绝并经再次检查和确认，才结束错误/过期邀请尝试。

`installed:true` 表示 HTTPS 协议/认证及一个 MCP 表登记完成；打开/重载 Codex，正常新 MCP 进程加载。它不会强杀旧 worker，也不意味着已运行模型自动重读了指令。`model_loaded:"unknown"` 表示此接口无法观测模型是否采用了指令；不能解释为已加载或未加载。旧回执的历史字段同样不是当前模型加载探针。再次运行安装命令会检查已装身份/范围，不再输入邀请。profile、policy、helper、scope 或配置漂移时停止并保留现场，不自动扩权。

安装与再次安装成功后，结果明确返回 `onboard_check.status:coverage-check-required`。这是给执行 Agent 的下一步交接：宿主工具可用后使用 `agent-brain-onboard` 检查所有已授权可读取记忆来源的覆盖；安装器不扫描私人文件、不启动模型任务、不上传记忆。旧身份、安装完成、旧 imported 标记或 pending=0 不能跳过覆盖核对。缺少覆盖记录、首次只导入一部分、后来新增记忆或新 Harness 来源时按实际能力枚举分页/批次，记录来源范围、指纹、已读/重复/排除/未检查/不可访问/冲突/待处理数量与原因。等价内容跳过写入；缺失、新增或差异才进入明确增量核对，差异保留双方独有信息并用当前 revision/CAS 协调。排除公司敏感、秘密、受禁历史与既有范围外材料，不按重要性或三主题检索预算缩小已有记忆覆盖。普通重连不自动再导入，覆盖检查也不重复配对。

安装协议检查只读，不生成内容映射，因此安装瞬间映射数可为 0。MCP 正常初始化后才刷新获授权映射；之后数量增加属于状态时序，不代表安装失败或记忆 Onboard 已完成。映射文件存在仍不证明模型采用了全部指令。

## 状态与实际使用

```sh
sh "$CHECKOUT_ROOT/tools/install-mac.sh" --action status
```

状态列出已装活动版本、队列各状态数量和自有映射数，不访问 Keychain、不请求 Server、不输出内容正文。它是本机完整性检查，不能替代在线使用验证。

`installation_present:true` 表示已有登记；`installed:true` 和 `installation_state:verified` 才表示该登记、原固定解释器、完整 MCP 表、policy/profile、bootstrap、回执及活动版本通过检查。运行 status 的 Python 可以不同于原安装解释器，它不会重写原 command。`installed:false` 配合 `present-review-required` 表示安装存在但某个完整性检查受阻，不代表未安装。安全 `reason` 和 `next_action` 指明检查点：例如 mcp-binding-changed 保留并恢复原表或审核替换计划，interpreter-unavailable 恢复原解释器，bootstrap-package-changed 恢复原校验包；不改摘要、不删缓存、不自动重装覆盖。没有登记和恢复材料才返回 client-not-installed；登记缺失但安装回执还在则提示恢复原安装。

在 Codex 明确要求通过 `agent_brain_server` 列出获授权的内容，读取一个 Memory 的 revision，再用该 revision 和稳定 request_id 更新用户指定的测试内容；仅 `persistence.durable:true` 确认远端持久保存。用同一个 request_id 重试未知结果，不能当作新写入。初次验收使用用户批准的测试 ID/内容，保存并恢复原值；不要擅自改真实内容、激活 Rule/Skill 或扩大权限。读取和写入采用 `content_catalog` / `content_read` / `content_put`；新增 Rule/Skill 默认 disabled，详见 [内容范围](PERSONAL-OWNER-SCOPE.md)。

同步在启动和工具调用边界发生；原生 Memory/Rule/Skill 映射完成后，已运行模型是否重新读取仍需观察。支持传输离线时读取已核验快照、持久排队和原 ID 重试；冲突保留，不能覆盖远端修改。

## 卸载

先在宿主的 Settings → MCP servers 中，只停用 `agent_brain_server`，保存并使用宿主提供的 Restart/reload 让配置生效，使本工具 MCP worker 退出；不要退出 ChatGPT/CodexCLI，也不杀进程。此精确停用只将本工具表的 enabled 从 true 改为 false，不能改 command、args、env、工具范围、摘要或其他 MCP。公开 status 会报告 content-native-client-disabled、registration_verified:true 和队列/映射统计，卸载仍检查完整原绑定和排他 lease。管理界面没有单服务停用/正常关闭能力，或 worker 未退出时停止报告，不以退出整个宿主兜底。宿主配置能力见 [官方 MCP 说明](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。然后执行：

```sh
sh "$CHECKOUT_ROOT/tools/install-mac.sh" --action uninstall
sh "$CHECKOUT_ROOT/tools/install-mac.sh" --action status
```

确认 `Continue` 后移除本工具 MCP 表、登记和哈希一致的自有 Memory/Rule/Skill 文件；只删自有指令块，保留用户指令与其他 Codex 设置。空的自有 Skill 目录移除；不递归删除目录。退役缓存的 owned/active 映射指针，使再次安装能够重建相同版本映射。`client-uninstalled` 表示入口登记及映射已移除；不能以仍存有恢复材料称已安装，也不能以仍活着的旧进程称卸载验证通过。

卸载后的 `status` 会只读校验完成回执及其中每个事务文件的实际后像，包括 MCP 配置、登记、自有映射和 owned/active 指针。匹配才返回 `client-uninstalled`、`uninstall_verified:true` 和核验数量。登记残留、文件被改回或后像漂移返回 `uninstall-state-review-required`、`uninstall_verified:false` 与安全原因，退出码非零；回执损坏或链接同样停止核验。后续用户对回执记录中的配置或指令文件编辑也会报告后像变化，需区分用户编辑与残留，不据此断言已经重新安装。状态检查不删文件、不修复、不创建锁或访问凭据；重复查询保留原现场。已完成的重新安装按新登记核验，不拿旧卸载后像判断。

默认保留专用 login Keychain 身份、固定 helper/bootstrap、依赖、profile/policy、安装及卸载前后像、outbox/imports 和完整快照，用于再次安装和安全恢复。它们主要位于 `~/.config/agent-brain-client`，原有安装也可能使用自己的已登记私有路径；不会卸载 Codex、改变 Server/cloud Data、撤销服务端设备或删除未同步数据。这是明确的可恢复卸载，没有“彻底擦除身份/数据”选项。

`client-in-use`：确认宿主只停用了本服务并完成 reload，等待该 MCP 正常退出后重跑；做不到就停止报告，不退出宿主、不杀进程、不删 lock。`uninstall-unconfirmed-writes`：保留安装，只重新启用本服务并 reload，通过原 MCP 重试原 ID 或协调冲突，队列无 pending/conflict/blocked 后再停用并重跑。取消后同样可只重新启用本服务恢复使用，不需要邀请码。未知队列、未完成导入、用户修改的自有文件或 Skill 目录新增私有文件会阻断移除，保持现场；不会盲删。

卸载每次实际写入/删除前重新检查该文件仍是自己的事务前像或后像；期间用户编辑会停止后续修改，保留编辑和恢复 journal，不能报告全部未改。shell 退出码 0 表示所请求的动作完成：uninstall 仅对 client-uninstalled/client-not-installed 成功；取消、未确认写入、活跃 worker 和 review-required 均非零，即使原安装仍存在也不会报成功。始终同时核对 JSON 的 status 与 native_changed。

## 中断恢复与再次安装

```sh
sh tools/install-mac.sh --action repair
sh tools/install-mac.sh
```

repair 继续自身中断的卸载，只接受事务前后像，外部修改停止并保留双方。首次安装/配对中断重跑带同一连接文件及摘要的原命令；已有身份的再次安装直接重跑无参数命令，复用已保存计划、原 identity/profile/helper 和同一缓存，按现有安装事务恢复。

无参数入口先核验现有登记或完成的卸载回执，再验证原 profile、发布政策、bootstrap、设备及 Codex 路径绑定。已有保存的 `first-install/connection.json` 必须与该绑定完全一致；旧安装没有此文件时，在内存中从保留元数据恢复同一连接描述，不生成凭证、不索取新文件、不改变信任锚。不可信、外部修改、未完成卸载或仅有孤立连接文件时报告 `connection-recovery-review-required` 并保留现场；显式指定的错误连接文件报告 `connection-file-review-required`，不会静默换用其他资料。若原身份不存在，现有安装事务停止，不转为新身份配对。

再次安装仅准备经验证的新固定启动器和精确 MCP 计划，保留原发布政策及信任锚，不另建身份，不发新邀请。在线认证通过后重建映射，再启动 Codex 验证读写。不要删除保留目录或 Keychain 项来“修复”。

## 当前实际限制

活动运行包沿用安装时固定的受信 HTTPS 渠道，main 提供可审核的安装/生命周期源码，不直接替代活动发布渠道。自动更新只在 MCP 正常启动时检查并激活兼容包，健康失败回滚；没有周期后台更新或运行中热切换，不自动更新固定 helper/bootstrap。

## 更新后继续 Onboarding

用户提出“帮我把 Agent Brain 更新到最新版，然后继续完成 Onboarding”时，Agent 使用公开仓库入口：

```sh
sh "$CHECKOUT_ROOT/tools/install-mac.sh" --action check-update
sh "$CHECKOUT_ROOT/tools/install-mac.sh" --action update
```

不传新 connection 或邀请。两项动作复用已验证登记中的原 policy、渠道、信任锚和固定依赖，不重新配对、不访问设备凭据、不改原生 MCP 设置。check-update 可以下载并校验候选缓存，但不激活、不运行候选健康检查；update 使用既有健康检查及回滚机制切换活动包。维护所需工具/解释器缺失或漂移时停止审核，不重新准备或改写其绑定。重复 install 的已安装分支只核验登记，不检查或应用软件更新，输出会指向上述入口。

check-update 成功返回 client-update-available/current 与 channel_checked:true；update 成功返回 client-update-updated/current。网络不可达、健康失败或信任/范围审核受阻均退出非零并保留恢复材料；不要把旧活动包版本称为已确认最新版，不通过改摘要或信任锚跳过审核。status 不访问渠道。

三个版本分别核查：active_package_version 是本地活动包，latest_channel_version 是本次成功渠道检查的候选，client_runtime.running_worker_version 是 MCP 调用所用 worker。CLI 无法观测 worker，其字段为 null；reload_required:true 表示活动包已变化，需要宿主正常新 worker，null 表示当前 worker 状态未知。通过宿主正常重载本服务，不杀进程；确认新 MCP 返回版本与渠道候选一致后再进行本轮 Onboard。即使更新回报 current，也不能据此断言旧 worker 已重载。main、tags、旧回执和映射数量都不能替代此核验。

完整当前授权目录使用 `content_catalog({"kinds":["memory"]})`（范围内也可加入 rule/skill），省略 query/since/until/limit。目录没有分页 limit；Memory-only query 别名和 memory_query 的 limit=1..3 只是检索，不证明全量。limit=100 在参数阶段拒绝，不发 HTTP。目录后按实际条目读取，记录每次结果、stage/HTTP 状态、取消/超时及重试，不把旧轮次 5 成功/18 超时或旧 3 次重试复制成本轮结果。尚未读完就报告实际剩余，不宣称已完成。

安全错误字段只给阶段和类别：parameters、transport、http、response-parse、response-validation、content-validation 或 client-policy-or-credential。HTTP 状态来自实际响应；transport 没有 HTTP 状态，不能单凭它认定偶发网络。解析/回执失败不归类为网络。读取参数错误不给 stable request_id/expected_revision 的写入提示；写入未知结果仍沿用原 ID 和 CAS 参数，不生成新 ID。报告不包含凭据、响应正文或记忆正文。

已确认 outbox 也占累计 1024 条上限，目前没有自动归档；达到上限会拒绝新请求，保留旧数据。Server 本地提交后 GitHub 推送未确认，会阻塞后续新写；当前 `storage_unowned` 也可能阻断缓存读取，须恢复原请求。256 条 catalog / 512 KiB snapshot 限制仍在。此版本不承诺无限写或弱网下始终可读。

源码和合成隔离测试不是独立实机验收。真实当前设备卸载/固定入口切换、Keychain、新邀请与发布渠道操作须先确认具体对象及影响；不要自动操作。安装成功后的实际 MCP 读写、卸载、再次安装由未参与开发的执行者仅按本文验证并记录版本、状态和数据保留。未实测另一台裸 Mac，不宣称完成裸机、签名、公证或 Gatekeeper 验收。
