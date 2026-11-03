# 文档索引

> 状态：当前维护；核对基线为 2026-11-03 的安装器／Windows 运行包 0.5.9。当前说明、版本证据与历史记录分开维护。

安装 Agent 继续先读根目录 [README](../README.md)、[AGENTS](../AGENTS.md)、[BOOTSTRAP](../BOOTSTRAP.md)，无需先遍历本文档树。完整正文已移入分类目录，每个主题只维护一份。22 个旧 docs 路径仅保留导读和原锚点；根安装入口已直接引用新正文，安装 Agent 无需经过兼容页。

| 分类 | 入口 | 用途 |
| --- | --- | --- |
| 安装与使用 | [安装索引](installation/README.md) | 平台首次接入、更新、恢复、卸载 |
| 架构与合同 | [参考索引](reference/README.md) | 公共协议、owner 范围、平台／宿主边界与兼容数据 |
| 开发与发布 | [开发索引](development/README.md) | 隔离检查、候选构建、正式发布流程 |
| 验收 | [验收索引](acceptance/README.md) | 按轮次记录与证据范围 |
| 当前版本 | [0.5 交付状态](releases/0.5-status.md) | 0.5.8／0.5.9 补记、基线核对、封版准备 |
| 历史归档 | [归档索引](archive/README.md) | 按版本保留历史发布、候选和验收语境 |
| 维护规则 | [维护与命名](maintenance.md) | 状态、命名、兼容路径、验证与变更清单 |

## 兼容入口与数据

根目录 [PROTOCOL](../PROTOCOL.md)、[ADAPTERS](../ADAPTERS.md)、[AUTOMATIC_MEMORY](../AUTOMATIC_MEMORY.md)、[GLOBAL_MEMORY](../GLOBAL_MEMORY.md) 保留原路径；后两者的内容和组织本轮不改。ADAPTERS 包含历史候选，当前安装使用平台步骤，不能将旧候选当当前能力。

[connection.example.json](connection.example.json) 是无凭据旧连接格式示例，包含合成端点和占位锚，只供合同说明／旧事务兼容，不能直接安装；[FREEZE-4.0.json](../FREEZE-4.0.json) 是既有 4.0 不可变基线。二者及本地 [运行 manifest](../releases/manifest.json) 保留原字节与路径，不随文档归档或版本文案重写。

`python3 -B tools/check_docs.py` 离线检查所有文档与兼容页的相对链接／锚点；现有 Client checks CI 自动运行它及故障注入测试。维护规则见 [文档维护](maintenance.md)。
