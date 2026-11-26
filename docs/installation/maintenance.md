# Client 日常维护

> 当前维护。核对基线 Client 0.8.36，适用于 macOS／Windows 的受信安装入口，覆盖 Codex 与 CodeBuddy。

先按用户要处理的对象选择现有入口。日常检查复用原设备身份和授权；更新、队列恢复、启停及重装各有自己的回执。

1. **检查安装或更新。** 在独立的官方源码 checkout 运行本平台安装入口的 `status`。需要更新时先运行 `check-update`，核对实际渠道检查和候选，再运行 `update`。macOS 用 `sh bootstrap/macos/install.sh --action <操作>`，Windows 用 `& ./bootstrap/windows/install.ps1 -Action <操作>`；CodeBuddy 传对应宿主选项并沿用已登记界面。保留原策略、信任锚、凭据、来源范围、暂停及队列。参见 [Mac](mac.md)、[Windows](windows.md)和 [CodeBuddy](codebuddy.md)。
2. **核对在用版本。** 分别报告安装入口看到的活动包、成功检查的渠道候选和新鲜 MCP 响应中的 `client_runtime`。如需重载，只走宿主对 `agent_brain_server` 的正常流程，先让在途请求完成；模型加载、Hook trust 与实际 Hook 投递分别核对。
3. **恢复同步或修订内容。** 用 `content_queue_inspect` 核对待处理项，未知结果按原请求 ID 和参数恢复，CAS 冲突先读当前版本再审阅合并。获准编辑的 Memory／Rule／Skill 用 `content_read` 和 `content_put`，保留已有启停设置。明确要求启停时，用 `content_put` 的 `operation=set_enabled` 或现有本机管理页；只需同步本机映射时使用管理页的“拉取到本机”。分别核对 Server 保存、GitHub 备份、本机同步及模型加载。参见[队列与冲突恢复](../reference/memory-behavior.md#保存队列与冲突恢复)和 [Rule／Skill 管理](../reference/rule-skill-management.md)。
4. **检查自动采集与召回。** 使用已登记环境的[项目记忆](../reference/automatic-memory.md)或[全局记忆](../reference/global-memory.md)状态入口，分别核对本地启停、实际事件接收、待传队列、提取和召回。保留现有排除目录与项目隔离；用户明确要求时才执行相应的暂停或恢复。项目 `flush` 重放本机项目队列，项目 `sync` 请求 Server 同步，全局 `global-sync` 恢复获准的全局投递。保留原请求参数，并读回远端确认。

收到 `review-required`、所有权冲突或中断状态时，保留原文件、回执和队列，按平台说明使用原命令续接或执行适用的 `repair`。仅在已知布局不兼容或发布说明明确要求时按[重装流程](reinstall.md)处理。交接记录实际观察到的状态和未完成项，正文、凭据和私密路径留在设备内。

## 自动检查和升级

[0.8.36 发布说明](https://github.com/goodname427/agent-brain-client/releases/tag/installer-0.8.36-setup)要求从 0.8.35 进行一次常规手动更新并由宿主按新登记重连。迁入固定启动器后，兼容更新会等在途请求完成，在同一连接切换 Worker 并通知宿主刷新工具清单。用新鲜调用核对 worker、`reload_required` 和实际工具可见性。

从支持本功能的版本起，已验证的 HTTPS 安装默认启用设备级自动更新，包括用原 `update` 入口首次升级到该版本的设备。已有任何一个受管 MCP 连接时，通过原 stable 渠道约每小时检查一次。兼容候选通过现有校验后在同一连接交接，原有显式 `check-update` 和 `update` 始终可用。

| 操作 | macOS | Windows |
| --- | --- | --- |
| 查看 | `sh bootstrap/macos/install.sh --action auto-update` | `./bootstrap/windows/install.ps1 -Action auto-update` |
| 开启 | 加 `--update-policy enable` | 加 `-UpdatePolicy enable` |
| 永久关闭 | 加 `--update-policy disable` | 加 `-UpdatePolicy disable` |
| 暂停一天 | 加 `--update-policy pause-day` | 加 `-UpdatePolicy pause-day` |
| 暂停到指定时刻 | 加 `--update-policy pause --pause-until <带时区的时间>` | 加 `-UpdatePolicy pause -PauseUntil <带时区的时间>` |

指定时刻使用带明确时区偏移的 ISO 8601 格式，例如 `2026-12-01T09:00:00+08:00`。查看结果列出当前策略、活动包版本和最近一次后台尝试。实际 worker 版本仍由新鲜 MCP 响应确认。需要重连、重新授权或迁移的候选走原手动流程。

只要对应宿主的受管 MCP 连接仍在，已授权 Content 约每分钟对账一次，无需工具调用。软件升级关闭或暂停时对账照常进行。重连后，后台会用原请求重试尚未确认的 Content 写入，手动恢复入口仍可处理冲突。

明确授权当前宿主的可共享个人原生记忆和既有接收目标后，请宿主运行 Agent Brain Onboarding（`agent_brain_server.memory_onboard`）。工具返回批次 `approval_id` 后继续后台导入；用相同宿主、来源范围及接收目标，传入此 ID 和 `resume=true` 查询同批进度，无需再次批准或提交。接收计数来自 L0 回执，不代表提炼已完成。
