# 0.5.5：包含根目录入口的平台边界完整收口

本版本包含 0.5.4 的真实 MCP checkpoint 修复，并将根目录 `connect.py` 旧 HTTP 元数据发现也改为复用 `brain_lock.native_platform()`。旧 HTTP 分支仍先拒绝原生 Windows，不引入旧协议作为 Windows 安装回退，不扩大平台或授权。

源码平台审计覆盖全部生产 Python 文件，包括根目录；仅底层 `scripts/brain_lock.py` 和测试代码可使用原始架构发现。新回归实际调用旧入口的只读元数据发现并验证原 Windows 门禁，同时继续执行源码和构建运行包的默认 `remote_mcp.Server`、`ContentServer` 初始化及只读 installation check，在 Python 3.11 最小环境下确认空标准库架构不再阻断真实 checkpoint。

沿原 Windows stable ancestry 发布新不可变 0.5.5；0.5.4 及所有旧 tags/assets 保留。凭据源码摘要、文件集合、能力、原锚、身份、nonce、profile、依赖及恢复记录保持原合同，Mac stable 和生产策略／数据不变。不增加模型探针，也不宣称 Windows 桌面、ARM64 实机或 Codex/model 重载已成功验收。
