# 0.5.6：安装工具契约与安全恢复闭环

0.5.5 的计划并非硬编码三项数组：生成器只按 grant 取服务端方法，并手动排除 memory_query，因此 read+put 得到 content_catalog/read/put。运行包安装检查另加 request-ID 与四个本地来源工具，得到八项；真实服务还已声明 read 权限下的 memory_query。这三条路径没有共享完整能力定义，旧平台恢复又不会迁移普通旧工具计划。

工具契约 schema 1 现在由 content_mcp 的既有服务端方法、grant/内容类型和本地工具声明共同生成，计划与安装检查消费同一份定义；运行服务使用同一本地声明。移除过期 memory_query 排除，因此 Memory read+put 的当前完整集合是九项，只有 rule/skill 的 read 集合仍为 catalog/read/request-ID。read/put/remove、artifact/ID scope、原始发行锚、摘要和来源授权独立校验；不从远端返回的未验证列表生成授权，不新增 Hook、目录或未来采集授权。request-ID 只本地计算；来源工具仍需逐项选择和审查，Windows 旧 memories reader 仍 unavailable。

正常入口能将确认为未执行、原始前像匹配的旧三工具计划迁移到同一政策/包/命令/环境/身份下的新工具契约，写前日志保留旧计划和 pairing-attempt。正常重试还通过原发行策略更新独立运行包，保留固定 bootstrap；不依赖另行手工更新。各写入点中断后可恢复；应用中/完成/回滚事务和外部编辑仍走原恢复，不重写未知状态。已安装且有原始摘要认证的旧白名单只读检查兼容，不静默启用工具。

集成检查覆盖旧三对八的真实基线复现、新包真实 Server/ContentAdapter/installation-check 工具一致、正常向导全新安装、已登记旧计划恢复和写入中断；协议结果由构建运行包生成而不是复制期望计划。凭据/网络在该合成集成中作为外部边界替换；另外保留真实 localhost TLS/address-key 集成检查。没有模型调用、原生会话或用户设备状态访问；用户桌面 UI、ARM64 实机与 Codex/model 重载未实测。

沿原 Windows stable 发布不可变 0.5.6，原密钥/设备/nonce/profile、旧包、计划与日志、Mac stable、Server 政策和数据保留。用户重新走最新官方正常入口即可恢复，不需要手改 PATH、摘要、工具清单或重新输入接入密钥。
