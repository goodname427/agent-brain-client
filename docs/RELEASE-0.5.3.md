# 0.5.3 Windows 安装兼容性修复

Python 3.11 的 Windows `platform.machine()` 依赖 `PROCESSOR_*` 环境变量；安装检查使用严格环境时这些变量不存在，0.5.0 因而返回 unreviewed-platform。这不是 Python 3.11 普遍不支持 Windows。平台层改为通过 IsWow64Process2 取得实际宿主架构，保留 x64／ARM64 和原生 OS 拒绝边界。

Windows 安装与记忆管理入口共用 Python 选择器：跳过 Store 执行别名，验证实际解释器、Python 3.11+、标准库、TLS 根与可读取的可执行文件。已固定的依赖仍按原摘要检查。仓库属性、独立 clone 和 Git blob 字节检查保证 LF 字节契约，凭据源码及 manifest 校验保持严格。

已登记但安装自检失败的 0.5.0 设备可正常重跑新入口：从原受信渠道验证 0.5.3，保存旧 bootstrap／计划及恢复日志，只更新尚未执行计划的 bootstrap 绑定，继续原安装事务。接入密钥、设备凭据、nonce、profile、授权范围、发行锚和队列不变。任何已有原生回执或外部修改仍须原事务恢复。预登记的精确 CRLF 元数据另有保留前像的受限迁移；不接受任意摘要变化。

Mac 与 Windows 共用核心；Mac 渠道和既有安装保持原策略。Windows 运行包沿原 personal-https-windows-stable 渠道前向发布，不改旧包、旧 tag、文件集合或能力范围。CI 覆盖 Mac 完整隔离测试与 Windows Python 3.11／3.13 的原生边界及恢复。真实用户桌面密钥窗口、Codex 重载与模型加载仍由设备正常流程验收；不运行模型探针或读取原生会话。
