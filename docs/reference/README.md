# 架构与合同参考

> 当前参考；旧版本细节保留其版本语境。

| 文档 | 范围 |
| --- | --- |
| [公共协议](../../PROTOCOL.md) | 公共请求、回执、持久化与兼容语义 |
| [多平台开发](multiplatform-development.md) | 共用业务核心、Codex 适配、薄 OS 层及后续 Harness |
| [宿主配置写入与恢复](windows-host-config-writes.md) | 0.5.7 起保留安全描述符的原位写入、日志与恢复限制 |
| [owner collection](personal-owner-scope.md) | 0.2.5 起的个人 read/put 范围，历史邀请细节仅供兼容 |
| [Rule / Skill 启停](rule-skill-management.md) | 本机管理页、明确启停、跨端拉取与失败恢复 |
| [Client 记忆接口](memory-behavior.md) | 当前工具、来源和回执，正文与记忆政策本轮不调整 |
| [适配器历史参考](../../ADAPTERS.md) | 含旧签名组件、SSH、Notify 候选；不能整体视为当前合同 |
| [无凭据连接示例](../connection.example.json) | 合成旧格式，不是可用信任锚 |
| [既有 0.4 阶段固定映射](../../FREEZE-4.0.json) | 不可变阶段／组件映射，不是 0.5 新冻结文件；[历史标识说明](../releases/0.5-status.md#历史标识) |

[项目记忆入口](../../AUTOMATIC_MEMORY.md) 与 [全局记忆入口](../../GLOBAL_MEMORY.md) 维持原位置。本轮仅整理文档，不调整内容记忆组织、受管 Rule/Skill 或生产目录。
