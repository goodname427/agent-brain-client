# 验收与证据

> 当前维护；证据按版本、提交、环境与轮次登记，不复用历史结果冒充本轮结果。

当前 Windows 状态：用户确认本轮安装与验收通过。公开仓库没有设备架构和逐项回执，不能扩展为全部架构、桌面、Hook trust、模型上下文或异常恢复均已通过；见 [0.5 交付状态](../releases/0.5-status.md)。

- [Mac 更新／Onboard 逐轮模板](mac-install-onboard.md)：当前 Mac 入口使用此正文，旧路径仅作兼容导航。
- [Windows 安装阶段](../installation/windows-stages.md)：逐项记录 launcher、映射、worker/model、Hook 与来源批准；单一 tools/list 成功不能替代其余阶段。
- [0.2 时代跨平台配对记录](../archive/0.2/platform-pairing-acceptance.md)：历史上 Windows 尚未实现，不能再作为当前支持状态。
- [0.2 时代 Mac 收敛记录](../archive/0.2/mac-first-install-acceptance.md)：保留当时测试与未测范围。

每份新验收记录使用 `acceptance/<版本>-<平台>-<YYYY-MM-DD>.md`，至少记录准确源码 SHA、安装器／活动包／worker／渠道版本、OS 与架构、实际命令、通过／失败／跳过／未执行、授权范围与剩余待办。公开证据只保留非秘密元数据，不放入用户内容、真实路径、密钥或私有部署地址。

源码测试、原生 CI、用户设备操作、模型采用指令和生产 catalog 后验检查分别记录。生产 catalog 核对由既有独立任务处理，本轮不执行；文档索引和用户确认都不能替代它。
