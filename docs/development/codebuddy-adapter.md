# CodeBuddy 适配边界

> Client 0.7 开发说明；核对日期 2026-11-06。安装与实际能力见 [CodeBuddy 接入](../installation/codebuddy.md)。

适配复用 [共用核心](harness-core.md)，不增加记忆数据库、抽取器、Server 事务或凭据后端。查询／写入直接启动原验证后的 MCP 后端；采集通过原 GlobalClient 认证和来源门禁，直接使用共用 `brain_cache.EventQueue`，不加载 Codex 内容映射。CodeBuddy 采集单独绑定 `host_adapter` 和缓存路径，不替换 Codex 队列。

当前目录职责及进一步拆分见 [运行代码结构](runtime-layout.md)。以下行数保留首次实现时的审计记录。

正文内容是否适合记忆由 Agent 判断；Client 不再根据密码赋值、token、Bearer 或私钥头等文本形态推断敏感性。这同时适用于 CodeBuddy 的用户提示、助手正文和召回内容。明确的 LocalOnly／no-memory／private／不要记忆标记及禁止共享限制继续生效，来源批准、权限、停止门、事务和回执校验保持原样。明确私密的用户提示仍排除该轮两侧采集；未经标记的正文不会仅因文本形态连带排除助手回复。该调整不增加秘密扫描器，也不替代 Agent 对实际内容的判断。

## 厚度

首次实现时的物理源码为 **983 行**，不含测试与文档；行数包含注释、空行，不代表复杂度评分。

| 文件 | 行数 | 宿主职责 |
| --- | ---: | --- |
| adapters/codebuddy/host.py | 301 | JSONC 字段编辑、配置优先级、自有 MCP/Hooks/说明块，以及调用共用 host-file 事务和回滚 |
| adapters/codebuddy/runtime.py | 308 | 核对既有运行时、独立队列绑定、原生身份规范化、长文本分批、事件重试和召回输出 |
| adapters/codebuddy/transcript.py | 188 | 当前轮游标、IDE generation 消息引用、CLI 最终正文字段／JSONL 增量及正文读取边界 |
| tools/codebuddy.py | 122 | 安装／状态／协议检查／MCP／Hook 命令分发与冻结适配器核验 |
| 两个平台安装入口 | 64 | 调用原安装器并绑定 CodeBuddy，保留既有设备原生目录并升级旧共享运行时 |

复用部分包括 `brain_content` 的协议／回执验证、`brain_http` 的传输实现、`brain_memory.ContentOutbox`、现有系统凭据、发布校验、MCP 工具、全局来源核对与分层召回。`submit_turn` 保留给 Codex 的有界 prompt/final 配对；CodeBuddy 使用同一事件协议直接提交分批事件，以便在两种正文格式与长回复下保留真实文本，而不伪造配对内容。

没有复制记忆核心。存在少量本地路径、JSON 和代码 pin 核验，与原安装器相似；它们用于 CodeBuddy 冻结入口在载入共用运行时之前的检查。JSONC 编辑和宿主文件事务可供将来其他 JSON 配置 Harness 复用，但当前没有为了减少行数再引入注册表或通用宿主框架。

下一种 Harness 主要需要替换配置入口、事件身份／正文规范化、上下文注入与原生加载说明。无需重新实现 HTTP、CAS、持久回执或 outbox。CodeBuddy 的 transcript 路径和格式属于宿主实现证据，不应直接套给 DeepSeek。

## 验证范围

专项测试覆盖安装事务、配置冲突、共享 outbox、当前轮正文、敏感来源、长 Unicode 回复和断网重试。本地与 Actions 共用 `client_checks.py` 的清单，均包含 CodeBuddy 静态检查，Windows 清单也包含 CodeBuddy 专项和 PowerShell 入口解析。真实凭据 UI、IDE／CLI 模型加载与用户设备验收单列，不从合成测试推断。
