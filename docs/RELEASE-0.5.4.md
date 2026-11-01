# 0.5.4：完整 MCP checkpoint 的平台识别修复

0.5.3 的 Windows 原生平台层已修复，但 `remote_mcp.Server` 仍自行拼接 `sys.platform` 与 `platform.machine()`。`ContentServer` 的构造会进入这条默认 checkpoint 路径；Python 3.11 最小环境下仍会得到空架构并抛出 `Unsupported client checkpoint`。只验证 helper 或 `--bootstrap-check` 不能覆盖这个遗漏。

0.5.4 将安装器、默认 MCP checkpoint、Mac SSH/profile/component 检查和 helper 构建统一使用已有平台边界。唯一底层架构发现保留在 `brain_lock.py`，Windows 继续通过原生 API 获取架构；源码检查阻止其他生产模块重新调用独立的 platform/os 架构发现。显式 checkpoint 元数据的既有白名单、Mac-only 功能和不支持平台拒绝保持不变。

回归同时执行源码和实际构建运行包的默认 `remote_mcp.Server`、`ContentServer` 初始化与只读 installation check，在不传递 `PROCESSOR_*` 的最小环境中运行。已对照发布的 0.5.3 源码复现旧构造器错误；Windows Python 3.11 回归确认标准库返回空架构时实际新 checkpoint 仍可工作，不复制旧检测器或替换真实平台检查。Windows 3.11／3.13 与 Mac 继续共用同一测试核心，不增加模型探针。

已完成设备登记但安装未完成时，正常重跑新入口，保留原接入密钥、设备身份、nonce、profile、依赖及未完成计划。已验证运行包可以沿原渠道前向更新；它与不可变 bootstrap 的独立绑定分别检查，不因运行指针在重试间前进而要求重置身份。原生事务回执和外部修改仍走原安全恢复。

Windows 运行包以新不可变 0.5.4 沿原 stable ancestry 发布；不覆盖 0.5.3 或旧 tags/assets，不改凭据源码摘要、文件集合、授权、Mac 渠道、生产策略或数据。实际 Windows 桌面凭据交互、ARM64 实机和 Codex/model 重载未在开发环境实测；CI 的协议与系统边界验证不等同于实机成功。
