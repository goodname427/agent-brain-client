# 0.8：保留记忆，重新连接设备

0.8 使用新目录结构。已有旧安装应执行明确的 `reinstall`，不对旧布局原地 update，也不把普通 install 当成覆盖安装。新设备没有旧安装时继续使用普通 install。

本流程会建立新的设备连接。**旧身份和未同步队列不会自动接续或发送**；旧队列可能含有尚未上传的内容，它们只保留在归档中，不能声称已迁移到 Server。原 profile、pin、系统凭据项和旧文件不清理。Server 中已经保存的记忆不删除，原生 Codex 持久记忆留在原位置，不扫描或补传历史。

1. 从正式官方 main checkout 运行 `reinstall-check`（命令见下方）。它只检查当前登记指向的精确安装／内容租约，不要求所有 Client、IDE 或目录相关进程退出。不联网、不访问凭据、不创建锁文件、不修改原生配置。
2. 检查报告为 `fresh-install-runtime-in-use` 时，在对应宿主的 MCP 管理中**只断开 Agent Brain 服务 `agent_brain_server`**，并让正在执行的 Hook／事务自然完成；保留 IDE、其他 Client 和无关 MCP。Agent 可以使用已获授权、确实可用的宿主 MCP 管理接口完成这一步；安装器本身没有这种宿主控制接口。如果宿主不能单独断开该服务、占用者未知或旧 worker 不退出，保留现场并报告阻碍，不扩大到关闭全部宿主。再次运行检查；空闲后执行对应 `reinstall`，使用**原 Server、原账号**。安装器解释归档与重新连接行为后输入 `FRESH`。

   ```sh
   # Mac（Apple Silicon / Intel）：先检查，再明确重装
   sh bootstrap/macos/install.sh --action reinstall-check
   sh bootstrap/macos/install.sh --action reinstall --server 'https://your-server.example'
   # CodeBuddy IDE / CLI
   sh bootstrap/macos/codebuddy.sh --action reinstall-check
   sh bootstrap/macos/codebuddy.sh --action reinstall --server 'https://your-server.example'
   ```

   ```powershell
   # 原生 Windows（amd64 / arm64）：先检查，再明确重装
   & ./bootstrap/windows/install.ps1 -Action reinstall-check
   & ./bootstrap/windows/install.ps1 -Action reinstall -Server 'https://your-server.example'
   # CodeBuddy IDE / CLI
   & ./bootstrap/windows/codebuddy.ps1 -Action reinstall-check
   & ./bootstrap/windows/codebuddy.ps1 -Action reinstall -Server 'https://your-server.example'
   ```

3. 在本机隐藏输入中重新填写该 Server/账号的长期接入密钥。不要把密钥内容放进聊天、命令行或环境变量。已获授权的本机 Key 文件可通过 `--access-key-file`（Windows `-AccessKeyFile`）传入路径，免手动输入。安装器从该 Server 正常取得授权与发行信任信息；不会复用旧设备身份、伪造 pin、创建访问密钥或修改 Server 授权。账号或授权范围与原记忆不符时停止，不创建替代设备掩盖问题。
4. 核对输出中的 `retained_archive`、`memory_accessibility` 和 `installation_stages`。旧业务状态保留在 `~/.config/agent-brain-retained/<实例标识>/`；Python 和官方源码 checkout 等准备环境仍保留供当前安装使用。不要删除归档；其中的旧队列不由新安装自动读取或重放。
5. `known-memory-readable` 表示新设备已读取原来已知的记忆；没有旧的受管记忆 ID 时，`memory-catalog-readable` 仅表示新设备可读取记忆目录，不是完整性或 Onboard 覆盖率声明。已核验的本地 Memory/Rule 投影与受管文件归属由各自所有者保留并恢复，原生记忆文件不变。记忆缺失、网络失败或账号不符时保留现场，并按同一命令恢复，不能将失败当成迁移成功。核验结果按新 profile 摘要持久记录；在成功前，重启宿主、直接运行 MCP、映射命令或 Hook 都不能越过同一闸门产生映射或采集。
6. 原 MCP 暂停状态、采集启停开关、`global.off` 停止标记、`all_projects` 范围、排除目录、项目隔离／共享规则、非项目目录选择及个人记忆 recall 开关都会保留。旧单项目的 `memory.off` 或关闭状态会转为新设备的项目根目录排除项，包含其子目录；不复制旧项目身份、队列或可执行配置。限制落盘前，新采集由恢复记录阻止，中断后重跑同一安装入口恢复。原本暂停或停止的采集不会自动恢复，原窄范围不会扩展为所有项目。需要恢复某个旧停止项目时，明确配置新的来源范围，按原流程确认排除项变更；`global-resume` 不会清除该项目排除项。旧设备批准摘要和身份不复制，已启用的原范围由新设备通过正常流程登记。逐项完成 Mapping、未来项目采集与宿主 Hook trust 检查；只在观察到 worker 版本与活动包不一致时正常重载 Agent Brain MCP。模型加载与全量记忆覆盖仍单独报告。

中断后重跑**同一个入口、同一个 Server**。归档移动和宿主入口修改有独立恢复记录；不会再次归档半成品或重新发送旧队列。运行中 worker、原生配置外部改动、陌生入口、损坏的回执或未完成原生映射事务会要求处理具体冲突；保留原始文件，不能通过删数据、改摘要或套用示例配置继续。

新安装只是重新连接客户端；Server 存储迁移及其备份由 Server 的正式发布流程负责。这里的自动核验只覆盖新设备能读取的已知记忆和保留的本地投影，不会擅自改变 Server 数据或权限。

已 `complete` 的重装记录是历史事务：`reinstall-check` 读取当前登记并检查当前租约；再次明确执行 `reinstall` 会重新规划、确认并建立新归档，原完成记录保留在 `~/.config/agent-brain-reinstallation/completed/<实例标识>.json`，原归档不改动。`prepared`／`detaching` 继续检查该事务的原租约，`archiving`／`ready` 返回 `fresh-install-resume-required`，重跑相同入口恢复原事务，不创建另一个归档。完成记录的记忆验证证明损坏时仍停止，不以新事务绕过验证。

Hook 回执包含原生配置的完整前后像，可能超过通用元数据的 256 KiB 上限。重装与入口移除使用 Hook 所有者已有的专用读取器，仍要求私有、无链接、单链接、无重复字段且不超过 4 MiB；未完成回执或当前 Hook 定义与回执不符时停止。原回执逐字节归档，不截断或改写摘要。旧策略文件清单与当前布局不符时，`status` 以 `client-layout-reinstall-required` 提示明确重装；这不是旧运行包已通过核验，也不允许改文件清单、改 pin 或原地运行旧布局。


## 占用证据与自动协调的边界

归档发生 OS 错误时，`fresh-install-archive-blocked` 输出具体操作、源／目标、系统提供的失败路径、`errno`／`winerror`、事务 ID 和已归档项。拒绝访问不能单独证明 ACL 错误或哪个进程持锁；不自动改权限、强杀、删锁或复制覆盖。真实原因要根据该输出继续核实。

部分归档后的 `reinstall-check` 返回 `fresh-install-resume-required` 是恢复状态，不是新的锁清单。保留 `current.json`、原归档和剩余源文件，从新的官方 checkout 重跑同一 `reinstall --harness codex --server <原地址>`（Windows 使用 `-Action reinstall -Harness codex -Server <原地址>`）。未完成事务不会再提示 FRESH、不会重新规划或重复移除入口；已移动但进度尚未落盘的项目依据原对象身份确认后继续。源和目标同时存在、对象身份改变或原生配置偏离已移除后的记录时停止，不能手动合并、回搬或删除。归档全部完成后才进入原 Server／账号的隐藏输入和新连接阶段。

`locks` 给出精确路径及 `absent`／`free`／`busy`；重装遇到竞争时给出 `blocked_lock`。`fresh-install-runtime-idle` 只是瞬时检查结果，不是全部迁移前置条件已满足的声明；重装仍逐把取得原有独占租约，不能用一次检查绕过运行中保护。权限、路径／所有权异常会进入 review，而不伪称 worker 占用。Windows 仍可能因其他文件的打开句柄而拒绝归档移动；这类文件操作失败也须保留现场、处理具体句柄，不能由租约空闲推断归档一定成功。

仅在租约繁忙时查询其文件使用者。Windows 使用 [Restart Manager 的资源查询](https://learn.microsoft.com/en-us/windows/win32/rstmgr/using-restart-manager-with-a-primary-installer)，输出 PID、系统报告的名称和进程启动时间；Mac 使用系统 `lsof` 查询同一文件，输出 PID 与名称。`resource_users` 是文件使用者候选，不等同于内核字节锁所有权证明，不能证明其 Harness、版本或可安全终止；结果明确保留 `lock_ownership_proven:false`。受限或查询失败标为 unknown；成功查询也可能没有可报告的候选，绝不把空列表当成空闲。PID 也不能作为之后发信号的授权。查询不读取命令行、环境、会话内容或凭据；不调用 Restart Manager 的 shutdown/restart，不发进程信号。

**0.8.6／0.8.15 没有安装器可调用的实例级退出协议。** 新版官方入口可以诊断旧租约，不能给旧 worker 补上端点，也不能保证在不使用宿主服务管理接口的情况下让它退出。禁止用目录匹配的进程清单、强杀、删锁或改 pin 代替。此次 patch 实现的是精确诊断与按服务断开的流程，不宣称已经实现无需宿主接口的全自动温和退出。

如果要让未来实例由 Agent 完全自动协调，最小协议还需：每个持租约实例登记随机实例标识、版本、Harness、PID 与启动时间、精确 cache/lease；通过本机受保护的控制端点接收绑定本次重装的 quiesce 请求；所有 MCP、Hook、后台投递及其启动入口共同执行持久维护闸门，停止接受新工作并阻止宿主自动拉起。已开始的持久写入自然完成，保留原请求 ID、待上传队列和游标，再释放租约并回执。安装器仍必须取得全部受影响的独占租约后才能归档，超时／未知旧实例则停止，取消与中断由同一事务恢复。仅加一个 PID 文件或向某个 Python 子进程发送退出信号不足以覆盖这些边界。旧版不提供兼容层，旧实例仍使用宿主正常的服务断开能力。


### 账号或授权核对未通过

`fresh-install-account-mismatch` 的 `mismatch_fields` 只列固定分类：
`server-origin`、`account-actor`、`capability-scopes`、`content-read`、
`content-kinds`、`content-ids`。它不输出密钥、账号值或内容 ID，
也不等于密钥无效。`write` 与 `put` 按公共协议视为同一写权限；
真实权限减少、账号变化和保留内容范围越界仍会停止。
本地恢复记录或保留策略损坏属于恢复检查失败，不伪装成账号不符。
遇到失败时保留原事务和归档，报告具体分类，不重放旧队列或修改账号校验。


### 地址安装续接诊断

`access-install-resume-required` 是地址安装入口的异常兜底，不代表账号不符，
也不能证明账号检查已经通过。`mismatch_fields` 缺失时不要猜测为权限别名问题。
新安装器给出固定标签 `stage`、`reason` 和 `credential_accessed`；不输出异常文本、
文件路径、账号或 Key。`reason` 为 `file-missing`、`permission-denied`、`timeout`、
`io-error`、`validation-failed` 或 `unexpected-error`。这些是分类，不是现场根因证明。
`stage` 区分恢复状态／地址校验、租约、依赖、凭据、Server bootstrap、连接校验、
保留账号范围校验、连接落盘与安装交接。安装内部返回的状态仍由原事务负责。

已有失败报告先离线审阅，不为取诊断盲目重跑安装。只需安装器版本／源码提交、
退出码、status、已有的 stage/reason/mismatch_fields、事务 phase，以及“事务 ID、
归档是否与此前一致”的布尔结论。不要发送 Key、完整 connection/grant、账号 ID、
本机路径或整个 journal。旧报告缺少字段时明确标记缺失；新版本不能追溯旧异常。
已有 stderr 若包含异常，应在本机删去密钥、路径、账号和地址后，仅提供固定错误类别。

复核具体阶段后，再确认原安装进程已退出、锁已正常释放；使用正式补丁 checkout 的
`reinstall-check` 确认原事务／归档和 ready 阶段未变，随后才以原 Server、原 Key 文件
及原 Harness 续接。同一状态再次失败即停止，报告脱敏分类。不得新建 FRESH、
删除锁、改权限、手改 connection/binding/journal 或重放旧队列。
