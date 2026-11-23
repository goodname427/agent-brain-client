# 工具声明与固定启动器更新

> 当前维护；适用本次分层源码的执行布局。旧版本迁移经过保存在 [历史记录](../archive/0.6/tool-binding-migration.md)。

`application/tools/catalog.py` 是内容工具名称、schema、描述和 grant 过滤的唯一来源。安装计划、只读安装检查和 MCP 的 `tools/list` 使用这一合同；日常记忆检索统一为 `memory_query`。代码组织变化不增加原 read/put/artifact/ID 或采集范围。

安装器先核验目标包的完整成员和摘要，再从它自己的 catalog 计算声明。同一实现复用已加载模块；不同目标包通过有限时、有限输出的离线描述入口读取，不能用检查器版本的猜测清单代替。此机制只接受当前完整执行布局；没有旧九项、十项清单或历史路径的兼容例外。对应测试是 `test_content_install`、`test_platform_checkpoint`、`test_architecture`。

`adapters/installation/launcher_update.py` 负责 Mac/Windows 当前固定启动器的事务和中断恢复。只读检查不改登记。显式更新沿用原策略、信任锚、解释器、身份、权限和暂停状态，保留第三方配置和原请求队列。

受管 HTTPS 的 Codex 和 CodeBuddy MCP 使用同一个标准库单文件入口持有 stdio 和会话。业务进程按需启动。更新时，原业务进程处理完在途请求并退出，新进程核对已协商的协议、工具和缓存合同后接收后续请求。连接保留原工具合同，结果不明的写请求返回未知结果，客户端不自动重放。空闲后业务进程释放缓存租约，下一次请求可以启动兼容的已验证包。

如果新业务进程拒绝已协商的合同，会话可重新启动最近一次通过握手的 worker。即使后续更新覆盖了发布日志中的前驱，启动器仍在会话内记住这一个已授权入口。重启前重新核验登记、完整包内容和合同，不创建额外包副本或持久记录。

0.8.31 的 HTTPS 工具契约为 4，已验证的契约 3 包仍按各自的清单检查。0.8.30 的后台检查可单独激活 0.8.31，保留旧登记和旧会话。普通更新完成登记后，旧会话仍可能在 Worker 空闲退出后失去传输。仅启动一个独立 stdio 连接成功，不能证明原会话已刷新。归档和恢复在原 schema-1 队列中保留请求和回执，旧 Worker 无法读取新方法的队列项。缓存握手标记为 `content-ledgers-v2`。写过生命周期请求的设备须用支持该合同的 Worker 读取和重放队列，并保留原队列文件。

维护路径校验活动包与受管工具登记。启动检查可缓存需要改登记或切换 Worker 契约的候选，留给普通更新完成。普通更新完成包和登记后，用原会话下一次正常调用核对 Worker 及会话工具表。由宿主在已接收请求结束后安全刷新原会话。宿主未提供适用刷新入口或刷新结果不可证实时，保留未完成状态。

从 0.8.27 更新到单文件入口时，正常更新事务迁移原登记，宿主使用原生操作重新连接一次。随后普通业务文件增改沿用已有登记，由更新器完整校验包成员和摘要。原生 Hook 命令仍通过已登记包和原权限验证活动快照。正式同连接验收使用首版后的下一次普通业务更新。

Mac 的隔离回归通过 `test_mcp_single_entry_release` 使用精确 0.8.27 运行包和本地候选。检出须包含 `client-0.8.27-macos` 对象，按开发检查说明隔离 `HOME` 和 `CODEX_HOME` 后运行 `AB_REQUIRE_SINGLE_ENTRY=1 PYTHONPATH=src:.:tests python3.12 -B -m unittest -v test_mcp_single_entry_release test_mcp_worker_handoff`。

`tool_discovery` 报告活动包的工具声明、登记过滤和被过滤工具。MCP 的 `client_runtime.offered_tools` 表示实际 worker 提供的工具；CLI 无法观察 worker 或模型时保持 unknown。活动包、固定启动器、正在运行的 worker 和模型采用指令是四个独立状态，不能互相代替。

Windows 检查正式 0.8.25 的已登记运行包时，先核验完整文件清单和每个成员，再以该公开导出的精确清单摘要确认已审阅的 MCP 描述入口。工具 catalog 及其领域源码与当前包相同时，复用当前的纯工具声明计算。旧包中的描述入口由精确清单固定，当前 worker 的启动或租约变化不影响此计算。其它运行包沿用既有目标包描述入口。

## 已运行的旧启动器与工具列表刷新

[#241](https://github.com/goodname427/agent-brain-client/issues/241) 在隔离 Mac Codex 中用正式 0.8.31 作为起点。从同一会话通过普通更新激活同契约候选时，保留原固定入口可以切到新版 Worker。将候选的固定入口变更并刷新登记时，原连接仍运行旧 Worker，空闲退出后下一次只读无法分派。旧程序无法在已协商连接上启用未来的 `listChanged`。Codex App Server 的 `config/mcpServer/reload` 由宿主控制端调用，当前请求没有线程或服务选择参数。Codex 和 CodeBuddy CLI 的 `mcp` 命令均未提供指定服务重新连接的子命令。参见 [Codex App Server](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md#mcp-configuration-reload)及 [CodeBuddy CLI](https://www.codebuddy.ai/docs/cli/reference#mcp-commands-mcp)。

Mac ARM64 用正式包和由草稿 `6d74943` 构建的隔离 0.8.32 候选做了同会话对照。宿主使用发行版本和默认工具刷新行为，模型端点及数据为本机合成夹具。每组只运行一次当前公开 macOS 更新入口，立即发起同一会话的下一轮模型请求。

| 起点 | Codex App Server 0.171.0-alpha.13 | CodeBuddy Code CLI 2.185.0 |
| --- | --- | --- |
| 0.8.31 | 原登记不变，下一轮模型可见 13 项，`content_read(include_archived=true)` 由 0.8.32 Worker 完成。 | Agent 执行一次更新，原登记不变。下一轮通过默认延迟工具发现归档和恢复，并用 0.8.32 Worker 真正完成只读调用。 |
| 0.8.30 | 活动包和 13 项登记已更新，下一轮仍由 0.8.30 Worker 完成，模型只有 11 项工具。 | Agent 执行一次更新，活动包和 13 项登记已更新，下一轮仍由 0.8.30 Worker 完成，工具搜索找不到归档与恢复。 |

0.8.31 到候选的固定入口摘要、工具契约 4 和缓存 ABI 相同。保留这些边界可复用现有连接的温切路径。Codex 对照中在更新前已经接收的读取正常返回，随后新一轮切到 0.8.32。CodeBuddy 的无关 MCP 在更新前后只初始化一次。0.8.30 初始会话持有契约 3 和旧缓存 ABI。已运行的旧启动器不会采用新版登记、工具表或缓存合同。跨契约继续需要宿主能在原会话内只刷新 Agent Brain，保留用户权限和在途结果。以上对照没有通过该跨契约场景，也未覆盖 CodeBuddy IDE 或 Windows 原会话。

CodeBuddy Code CLI 2.185.0 的隔离 Agent 也执行了一次正式 0.8.30 → 0.8.31 更新。以 `--mcp-config` 显式载入原生登记文件时，宿主随后只重建 Agent Brain 连接。立即发起下一轮时工具尚不可用。延后读回时，同一会话的只读请求返回 0.8.31、`reload_required=false`，模型可见 13 项工具。无关 MCP 保持同一次初始化。按默认方式加载登记时，立即调用返回旧 Worker，延后对照仍显示旧工具表且调用失败。更新轮内调用按名称定向的 `WaitForMcpServers` 时，工具表仍为旧版。当前该接口不能作为新版工具已可用的确认。参见 [CodeBuddy 工具参考](https://www.codebuddy.ai/docs/cli/tools-reference)。

预先运行支持动态工具声明的固定入口后，隔离原型在初始化时声明 `tools.listChanged`。原 Worker 处理完已接收请求并退出后，启动器验证新 Worker，发布工具表，再发 `notifications/tools/list_changed`。空闲时，新候选也能触发这一过程。相同连接的实际宿主结果如下。

- **CodeBuddy Code CLI 2.185.0** 在默认延迟工具模式下自动重列工具。实验的 Agent 按精确命令权限调用合成更新，下一轮通过 `ToolSearch` 和 `DeferExecuteTool` 在原会话调用新版只读工具。两轮共用一次 MCP 初始化及同一固定入口进程。参见 [CodeBuddy 动态工具更新发行说明](https://staging-codebuddy.tencent.com/docs/cli/release-notes/v2.53.0)。
- **Codex CLI 0.171.0-alpha.13 的 App Server** 默认收到通知后未重列。隔离实例开启实验 `mcp_tool_list_changed` 且原会话已授权确切新增名称时，下一轮同一线程的模型请求可见并调用新版工具。只更新磁盘白名单时，原会话重列后仍过滤该名称。原会话明确禁用的名称继续不可见。参见 [Codex MCP 列表刷新实现](https://github.com/openai/codex/blob/main/codex-rs/codex-mcp/src/connection_manager.rs)。

原型覆盖同一缓存 ABI 下的新增工具、在途只读完成和空闲切换。正式采用前需核对跨 ABI 时的缓存租约、迁移失败和回滚，并通过实际工具调用确认宿主已采纳工具表。已授权工具过滤及明确禁用需继续生效。CodeBuddy IDE、Windows 原会话和旧固定入口的一次迁移仍待实测通过。[MCP 工具列表通知](https://modelcontextprotocol.io/specification/2025-11-25/server/tools#list-changed-notification)

### 正式 0.8.30 的兼容边界

三个独立 Mac Codex 线程分别用正式 0.8.30 启动，再各走一次当前公开更新入口。测试候选均由 0.8.30 发布树构建，临时本地渠道标为 0.8.32。固定入口字节、原生登记和它的绑定摘要在每组更新前后相同。更新均退出 0，隔离活动包指向各自的候选。原线程的实际只读结果如下。

| 候选唯一的 Worker 差异 | 原线程首次读取与 4 秒后复查 |
| --- | --- |
| 同一工具契约 3、同一缓存 ABI，Worker 文件及包版本变化 | 两次均由候选 Worker 返回，仍为契约 3 和 11 项工具。 |
| 只换缓存 ABI 标记，工具契约和 schema 不变 | 两次均由 0.8.30 Worker 返回，`reload_required=true`。 |
| 只给 `content_read` 的 schema 加一个合成可选字段，名称、契约编号和缓存 ABI 不变 | 两次均由 0.8.30 Worker 返回，`reload_required=true`。 |

旧连接的两个兼容检查各自能阻止切换。公开更新入口只能准备和激活候选、维护登记。已运行的固定入口拥有 stdio，并保留初始化时的工具契约和原生绑定。用契约 3 的兼容 Worker 可以移交原连接，但原模型仍只有 11 项。要让旧会话采用契约 4，当前受测宿主需要提供原会话内针对 Agent Brain 的刷新能力。

0.8.32 的默认路径继续使用现有同契约温切。[独立评审补丁](https://github.com/goodname427/agent-brain-client/commit/0268f7c0fc83c8657b8df798a3effa269df20923)的默认行为保持原工具合同。提前部署且由适配层接入动态能力的固定入口，仅在协议、缓存身份和 ABI 相同且旧工具 schema 保持时接纳新增工具。它等待在途请求结束，验证并接纳新 Worker，再发布工具表和通知。已预先部署该原型的 CodeBuddy CLI 用默认用户登记与默认延迟工具模式实测，Agent 完成合成更新后原会话下一轮调用新增只读工具，整个过程只有一次初始化。默认 Codex 在预先允许两个合成名称的条件下仍未重列。补丁覆盖固定入口的协议和请求交接。正式接入还须维持各宿主的原有授权和登记校验，并通过实际调用证明采用了工具表。跨缓存 ABI 仍需安全迁移路径及宿主支持。


### #269 工具表更新与原会话

支持工具表更新的固定启动器在 `initialize` 声明 MCP `tools.listChanged`。通过协议能力、工具协议版本、缓存身份和 ABI 校验后，启动器等待已接收请求完成，启用新 Worker，更新 `tools/list` 并发送 `notifications/tools/list_changed`。宿主继续使用缓存工具表时，既有且获准的兼容调用仍可直接由新 Worker 处理。工具退出当前 Worker 或受管登记后，缓存调用会收到 MCP 错误。新必填参数缺失时按新契约返回错误，原请求参数原样交给 Worker。

普通更新可以同时提交新包和新工具登记。保持同一策略、受管宿主命令身份和已验证启动器字节时，旧进程继续使用该连接，并等待新登记和活动包通过验证。已经运行的旧版启动器仍使用原始规则。首次迁移需从宿主读取新的 MCP 登记并连接一次；之后的兼容更新复用同一连接。宿主是否已重新发现工具需读回确认。宿主尚未采纳变更通知时，按宿主支持的 MCP 刷新流程操作。
