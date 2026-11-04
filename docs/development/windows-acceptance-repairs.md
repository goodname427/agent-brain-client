# Windows 验收修复

> 状态：源码修复，未发布、未完成 Windows 实机回归。核对日期：2026-11-04。发布与设备状态须单独报告。

## 采集关闭、空范围与设备拒绝

安装 Hook 不等于批准来源。`global-status` 现在逐项报告 `capture-disabled`、`source-scope-empty`、`local-source-approval-required`、`server-source-approval-unconfirmed` 和 `local-stop-fence`；默认检查不访问凭据或会话。

Windows 正常设备没有 Server SSH 管理器。已批准精确范围后，`global-configure` 会自动准备绑定本设备的 schema-2 元数据包并保持关闭，而不是要求普通 Agent 执行管理操作。管理端仍使用既有 `manage_global_memory.py prepare/apply`，不得借用另一设备身份、改信任锚、增加预算或导入历史。

显式检查 Server 登记：

```powershell
& ./tools/memory-windows.ps1 global-status --check-server
```

此命令使用已有 HTTPS 身份，发出 `memory_global_status`、`params:{}` 的只读元数据请求，返回本设备是否已登记和已有个人池 ID；它不启用或恢复来源。老 Server 不支持空参数时会明确返回错误，不把错误当成未登记。仅凭 `global_source_device_denied` 不能区分“未登记”和“身份/授权不匹配”。

若是从未批准、无任何范围的空安装，可显式运行 `global-install --profile-id <Server 返回的个人池 ID>` 选择已有池。已有来源、批准摘要或旧项目路由时拒绝换池，保持原队列与账本。

Windows 正常安装向导的空来源分支先用现有设备身份只读查询 Server 的既有个人池，确认是本设备且尚未登记来源后，把该池放入将要展示和确认的范围。查询失败、设备不符或 Server 已有该设备来源时停止并要求核查；不会凭本地随机占位 ID 创建新池或覆盖 Server 原来源。用户取消时不写入该选择。来源确认仍须针对实际目录、排除项和接收者在设备上完成，Server operator 另行审查并应用精确设备包。

`global_entry_installed=false` 且原生 Hook 与私有回执不一致时，保留两者并检查差异；不要删除回执、手改摘要或直接重装来覆盖外部改动。此现象的原因不能仅凭状态布尔值确定。诊断和恢复必须先核验现有事务及配置前像；原生宿主的 Hook 信任仍需正常审阅。

用户已要求所有项目、无排除时，为实际存放项目的每个本地盘根传入 `--include-root`，不设置 `--exclude-root`；例如 `--include-root C:/` 仅覆盖该盘下的项目。不要据此猜测其他盘，或添加 `--non-project-root`。先检查精确预览，再使用该预览摘要准备元数据包，交由已有 Server operator 应用，最后 `global-configure --activate-packet <原包>`。激活先核对 Server 的池、设备与范围摘要；拒绝时保留错误代码和本地关闭状态，不扩大权限。

## 普通 MCP 的 L1/L2/L3 读取

新增只读 `memory_recall`，参数为当前 `origin`、独立 reader `session_id`、`query` 和可选 `limit:1..3`。它通过当前 HTTPS 设备身份调用 Server 的 `memory_global_query`，保留现有逐设备/逐来源批准、个人池与项目隔离。普通 `memory_query` 继续查询 destination topics。

结果保留 `namespace_results[].recall.L1/L2/L3`、已提交事件、生成证据与 `source_context`。拒绝未经来源证明的行、L0 原始事件、错误设备、伪造适用范围和超量结果。跨来源数据保持 `reference-only`，不能升格为 Rule/Skill；返回空层不能宣称该层已读到。服务端拒绝不自动重试、配对或启用采集。调用不刷新映射、不补传队列、不触发模型，不使用 synthetic sandbox 证明正式召回。

## 本地验证与发布边界

新增回归位于 `tests/test_windows_acceptance_repairs.py`；现有分层召回、安装/升级测试继续执行。刷新、备份与并发路径由对应修复独立维护。使用隔离 HOME/CODEX_HOME，检查不读取真实记忆或会话。

按[固定发布流程](release-pipeline.md)正常提交和发布。Actions 因额度未运行须报告；本地测试不能伪造远端必需检查，不改门禁或开通付费。构建成功只证明候选包，实际发布号和运行 worker 版本以完成的渠道/设备回执为准。
