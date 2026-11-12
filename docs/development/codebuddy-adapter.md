# CodeBuddy 适配边界

> Client 0.7 开发说明；核对日期 2026-11-06。安装与实际能力见 [CodeBuddy 接入](../installation/codebuddy.md)。

适配复用 [共用核心](architecture.md)，不增加记忆数据库、抽取器、Server 事务或凭据后端。查询／写入直接启动原验证后的 MCP 后端；采集通过共享 CollectionClient 认证和来源门禁，直接使用共用 `adapters/storage/content.py` 的 FileEventQueue，不加载 Codex 内容映射。CodeBuddy 采集单独绑定 `host_adapter` 和缓存路径，不替换 Codex 队列。

当前目录职责及进一步拆分见 [运行代码结构](architecture.md)。职责以调用和依赖方向核验。

正文内容是否适合记忆由 Agent 判断；Client 不再根据密码赋值、token、Bearer 或私钥头等文本形态推断敏感性。这同时适用于 CodeBuddy 的用户提示、助手正文和召回内容。明确的 LocalOnly／no-memory／private／不要记忆标记及禁止共享限制继续生效，来源批准、权限、停止门、事务和回执校验保持原样。明确私密的用户提示仍排除该轮两侧采集；未经标记的正文不会仅因文本形态连带排除助手回复。该调整不增加秘密扫描器，也不替代 Agent 对实际内容的判断。

采集与召回共用明确共享限制判断：`[no-share]`、`[project-only]` 等行首标记，以及有清晰指令边界的行首禁止共享／仅限项目指令。正文中的引用、解释和“为什么规则禁止跨项目共享？”等普通提问不改变共享范围；“请不要跨项目共享这条信息，可以吗？”仍是明确限制。没有显式标记或明确前缀的自由表达交由 Agent 判断，Client 不做全文关键词或问号猜测。来源和权限核验独立于正文判断。

## 职责

| 位置 | 职责 |
| --- | --- |
| `adapters/harnesses/codebuddy/configuration.py` | JSONC、配置优先级、自有 MCP/Hooks/说明块，调用共用文件事务 |
| `adapters/harnesses/codebuddy/events.py` | 原生身份、长文本分批、当前轮事件与重试 |
| `adapters/harnesses/codebuddy/transcript.py` | 当前轮游标、IDE generation、CLI JSONL 增量与读取边界 |
| `interfaces/cli/codebuddy.py` | 安装、状态、协议检查、MCP、Hook 的输入分发 |
| `runtime/host_binding.py` | 校验既有共享运行库与原登记，绑定共用传输和来源服务 |
| `bootstrap/{macos,windows}/codebuddy.*` | 解释器和官方安装入口的顺序协调 |

冻结包包含当前完整 Python 运行库，逐项记录并验证摘要，可脱离源码 checkout 启动。共享运行库和正在执行的 Harness 包必须匹配；普通更新在共享代码刷新后同步刷新已经登记的 CodeBuddy 绑定；也可重新运行官方 CodeBuddy 安装入口刷新绑定，不加载不匹配版本，也不改设备身份、队列或来源范围。

跨宿主更新顺序由 `runtime/updates.py` 的组合端口协调：Codex 适配器只刷新自己的 Hook，CodeBuddy 从同一已验证活动包的隔离进程刷新自己的绑定。两端仍各自使用原有锁与文件事务，失败保留可重试状态，不宣称跨宿主原子提交。

Windows 网关使用继承原始 stdio 的子进程并透传退出码，POSIX 仍使用 `execve`；不缓存或改写 MCP 帧。本轮不包含其他性能优化。

协议、CAS/outbox、回执、传输、来源门禁与召回算法只有一份源码。Codex 使用共用 `submit_turn` 配对 prompt/final；CodeBuddy 在同一事件和队列合同上提交有界正文批次，以保留真实长文本。物理行数不作为适配边界的质量证明。

下一种 Harness 主要需要替换配置入口、事件身份／正文规范化、上下文注入与原生加载说明。无需重新实现 HTTP、CAS、持久回执或 outbox。CodeBuddy 的 transcript 路径和格式属于宿主实现证据，不应直接套给 DeepSeek。

## 验证范围

专项测试覆盖安装事务、配置冲突、共享 outbox、当前轮正文、敏感来源、长 Unicode 回复和断网重试。本地与 Actions 共用 `client_checks.py` 的清单，均包含 CodeBuddy 静态检查，Windows 清单也包含 CodeBuddy 专项和 PowerShell 入口解析。真实凭据 UI、IDE／CLI 模型加载与用户设备验收单列，不从合成测试推断。

### Current IDE transcript boundary

The CodeBuddy IDE native transcript can live below the OS-specific
`CodeBuddyExtension/Data/<user>/<IDE>/[uid]/history/<workspace-MD5>/<session>/index.json`
layout, in addition to the previously recognized CLI/native cache paths. The
adapter accepts only the Hook-provided exact session path and generation; it
never searches history to guess missing Hook fields. Only that generation's
completed assistant message references are read. Missing/unrecognized transcript
paths report `transcript-unavailable-direct-stop-only`, not transcript readiness.
Windows native-source I/O supports long local paths without changing OS policy;
link/reparse and hard-link rejection remain in effect. A synthetic regression is
not proof that a particular installed IDE emitted its Hook payload correctly.
