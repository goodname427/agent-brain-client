# 服务协议 1

认证为获授权 Bearer token，服务器 grant 固定身份与 read/write。只接受 HTTPS 或 SSH 隧道回环 HTTP，重定向不转发凭据。请求 POST /v1/requests，JSON 为 request_id/method/params。request_id 为稳定的 16–128 字符；重试同一写入保持 ID 和完整参数。

topic(id)、recall(query,limit) 只读；remember(id,title,description,body,expected_revision,type)、forget(id,expected_revision) 写入。新主题 revision 为 missing；旧主题必须使用读出的 revision。客户端不能传 Core 路径、actor、凭据、项目或 local_only。写头声明 X-Agent-Brain-Adapter=codex、X-Agent-Brain-Protocol=1、X-Agent-Brain-State-Schema=1；它们是兼容信息，不授予身份或权限。

只有响应 persistence.durable=true、state=remote-confirmed 才说明该请求已确认远端保存。CAS 冲突重读协调，用新请求 ID；网络/503 中断保留旧 ID 重试，不能声称尚未发生写入。client_upgrade_required 说明版本不兼容；storage_unowned 或 operation_uncertain 要保留现场交 Agent 处理，不重置或强推。

client_updates(adapter,platform,installed_version) 只读发现状态、受信 release manifest/commit 和兼容信息。未配置时为 not-configured；配置后为 current、compatible-update、adapter-review-required、release-review-required 或 release-unavailable，并给 next_action。服务目录检查不激活代码；客户端只用已经固定的 policy origin/ref/anchor 和 SHA256 检查同一发布，不把服务返回的来源当授权。桥接由固定 startup 传入逻辑 Harness 平台与已验证版本，不能把执行桥接的 Linux 服务器冒称 Mac Harness。数据提交与客户端发布分开。

stdio MCP 桥接器提供相同五个方法；记忆写工具必须由调用者提供 request_id 和 expected_revision，MCP JSON-RPC id 不充当跨进程幂等 ID。工具 arguments 不能传 token、路径、actor 或作用域。工具执行失败用 isError=true 和原服务 error/durable=false 返回，传输未确认时保留原参数重试。initialize/initialized 与 tools/list/call 只做协议协商；clientInfo 和 _meta 不授予身份。它不宣称 HTTP API 已实现 MCP Streamable HTTP，也不自动安装 native 配置、启动调度或迁移真实数据。

personal模式的每个请求额外声明X-Agent-Brain-Data-Mode=personal，仍要求Codex/API1/schema1；不合约调用在数据读写前拒绝。固定personal policy绑定独立root/origin/branch/actor及导入manifest SHA。import-only只接受清单内稳定request_id与完整请求摘要一致的remember，expected_revision必须missing；explicit-personal才允许其他已授权个人写操作。固定清单导入始终不覆盖已有主题，失败保留同一清单、请求ID与服务回执恢复。策略、身份、真实内容采集范围不会由请求参数授予。
