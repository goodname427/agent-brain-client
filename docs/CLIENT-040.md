# 0.4 本地候选：构建、安装、更新与受限任务

本候选增加受限提取与两批精确跨会话召回，保留现有配对身份、安装事务、缓存、CAS outbox 和更新回滚。运行需要 Python 3.11+；不改已发布 0.3.1 manifest，不自动发布、启用 Hook 或调用模型。

统一入口是 `python3 tools/agentbrain_040.py`（从本源码目录运行）。

1. 构建：`build --output /absolute/path/new-client-040 --version 0.4.4`。仅构建本地未发布包，不安装、不下载依赖或读取凭据。
2. 准备安装或固定启动器交接：`plan --device-home /absolute/device-home --package /absolute/path/new-client-040 --release-policy /absolute/private/existing-policy.json --release-policy-sha256 SHA256 --output /absolute/private/install-plan.json`。已有登记加 `--replace-existing`。只准备精确前后像计划；保留其他 MCP、指令、队列和身份。
3. 由设备所有者审阅计划后，`apply --plan-file /absolute/private/install-plan.json --plan-sha256 SHA256`。复用原安装事务和协议检查，不创建新凭据或跳过信任；中断继续同一计划，不删除证据重装。原政策若不允许此包，需发行者提供同范围、同信任来源的兼容发布，不能改 hash 绕过。所有真实操作属于最后统一验收。
4. `status --device-home /absolute/device-home` 查看现有状态。常规渠道更新用 `check-update` / `update --device-home /absolute/device-home`；只复用原登记与政策，不接收替换 profile。未发布的本地包不会被假报为渠道最新版；没有渠道发布时不能通过 update 获得本候选。

提取或精确召回使用 `task-check` / `task-execute`，另传 `--https-profile`、`--https-profile-sha256`、`--device-task-policy`、`--device-task-policy-sha256`、`--task-request-file`、`--task-request-sha256`。task-check 只核对元数据；task-execute 经现有设备凭据发送一次精确请求，无自动重试或排队。政策必须固定实际 actor/device、端点、profile 摘要、有限时间和请求；只读精确召回允许 extract_request=null。Server 再次检查独立会话、绑定、来源、预算与费用授权。不得填造会话或来源 ID。

普通 MCP 的 catalog/read/put、Memory 查询及来源审阅不受影响。提取不是普通工具的隐式副作用。自动采集使用既有显式 `client_startup.py --event-hook` 路径，仅支持确认兼容且已信任的原生宿主；新增定义不保证旧会话热加载，不另开影子订阅进程。未知/不支持的 Harness 保持明确 adapter-review-required，不能通过改名称或借用 Codex 身份接入。

离线继续读取已核验且身份绑定的快照，返回 stale 标记；写入保留原 request_id/CAS，重连恢复，冲突保留。原生事件 pending 不由普通 refresh 偷偷上传，须在当前有效采集授权下精确恢复。未知 Core 写入结果保持待对账，不重发换 ID；本地保存、Server 接受与 GitHub 备份分开报告。已确认队列总量仍有 1024 条上限，需要容量审阅时不删除历史凑通过。

本地/云编排是能力条件；本地 cwd、This computer 或 dot 不能证明模式。模式未知不妨碍构建和普通内容服务开发，但不能宣称原生 Hook 已受支持或实际加载。
