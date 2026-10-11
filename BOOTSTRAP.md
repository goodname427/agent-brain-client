# Client source bootstrap

先读 README.md 和 PROTOCOL.md。这里仅维护 Client 源码；安装必须使用审核后固定的发布源、manifest、运行配置、设备范围和安装计划。不要运行旧混合仓的 brain.py/sync，也不要从旧仓拉取源码。

验证源码时使用隔离 HOME 与 CODEX_HOME，测试只访问合成 localhost。构建不会证明模型已经加载 MCP、Keychain 提示通过或已部署。保留原生工具工作流；不读取原生会话或数据库。
