# CodeBuddy 适配边界

> Client 0.7 开发说明；核对日期 2026-11-06。安装与实际能力见 [CodeBuddy 接入](../installation/codebuddy.md)。

适配复用 [共用核心](architecture.md)，不增加记忆数据库、抽取器、Server 事务或凭据后端。查询／写入直接启动原验证后的 MCP 后端；采集通过共享 CollectionClient 认证和来源门禁，直接使用共用 `adapters/storage/content.py` 的 FileEventQueue，不加载 Codex 内容映射。CodeBuddy 采集单独绑定 `host_adapter` 和缓存路径，不替换 Codex 队列。

当前目录职责及进一步拆分见 [运行代码结构](architecture.md)。职责以调用和依赖方向核验。

自动采集只处理已批准来源中观察到的提示和已完成的助手回复。LocalOnly、no-share、project-only 等标记按正文原样提交。来源排除、停止与撤销通过来源设置执行。保留先前已排除的观察记录，也保留已观察的 generation 和传输回执。

召回继续使用明确共享限制判断，包括行首标记或明确的禁止共享、仅限当前项目指令。引用、解释和一般问题不会改变共享范围。

## 职责

| 位置 | 职责 |
| --- | --- |
| `adapters/harnesses/codebuddy/configuration.py` | JSONC、配置优先级、自有 MCP/Hooks/说明块，调用共用文件事务 |
| `adapters/harnesses/codebuddy/events.py` | 原生身份、长文本分批、当前轮事件与重试 |
| `adapters/harnesses/codebuddy/transcript.py` | 当前轮游标、IDE generation、CLI JSONL 增量与读取边界 |
| `interfaces/cli/codebuddy.py` | 启动 MCP、接收原生 Hook，为统一安装器提供绑定、状态及协议检查 |
| `runtime/host_binding.py` | 校验既有共享运行库与原登记，绑定共用传输和来源服务 |
| `bootstrap/macos/install.sh`、`bootstrap/windows/install.ps1` | 共用安装、更新和维护流程，通过宿主参数选择 CodeBuddy |

冻结包包含当前完整 Python 运行库，逐项记录并验证摘要，可脱离源码 checkout 启动。共享运行库和正在执行的 Harness 包必须匹配。普通更新在共享代码刷新后同步刷新已经登记的 CodeBuddy 绑定，沿用原设备身份、队列和来源范围。

跨宿主更新顺序由 `runtime/updates.py` 的组合端口协调：Codex 适配器只刷新自己的 Hook，CodeBuddy 从同一已验证活动包的隔离进程刷新自己的绑定。两端仍各自使用原有锁与文件事务，失败保留可重试状态，不宣称跨宿主原子提交。

Windows 网关使用继承原始 stdio 的子进程并透传退出码，POSIX 仍使用 `execve`；不缓存或改写 MCP 帧。本轮不包含其他性能优化。

协议、CAS/outbox、回执、传输、来源门禁与召回算法只有一份源码。Codex 使用共用 `submit_turn` 配对 prompt/final；CodeBuddy 在同一事件和队列合同上提交有界正文批次，以保留真实长文本。物理行数不作为适配边界的质量证明。

下一种 Harness 主要需要替换配置入口、事件身份／正文规范化、上下文注入与原生加载说明。无需重新实现 HTTP、CAS、持久回执或 outbox。CodeBuddy 的 transcript 路径和格式属于宿主实现证据，不应直接套给 DeepSeek。

## 项目 Hook 回执

项目回执包含同一份本地设置的原始、目标和可能的 Windows 回滚镜像。每份宿主镜像的上限为 1 MiB。回执读写上限覆盖三份镜像的 Base64 展开，再预留 1 MiB 用于两套 Hook 定义、路径和事务字段。该上限用于项目固定 Hook、状态、停止、更新和登记恢复读取的既有私有回执。常规私有元数据仍使用 256 KiB 上限。

0.8.29 能成功登记大项目设置，但固定 Hook 读取之前写出的回执时会触及旧上限。0.8.30 的项目状态和停止也依赖该回执。修复沿用现有格式及文件校验，普通更新后即可读回原设置摘要和回滚镜像。

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
