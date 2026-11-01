# Agent Brain 5.0 Windows Codex

本阶段提供 Windows Codex 接入，Client 组件版本为 0.5.0。支持原生 Windows x64/ARM64 的固定安装、升级、工具接入及获准未来会话的采集、提取和相关召回。具体使用 [Windows 安装步骤](FIRST-INSTALL-WINDOWS.md)；架构复用、同步开发发布及后续 Harness 合同见 [多平台开发经验](MULTIPLATFORM-DEVELOPMENT.md)。

共用核心负责安装事务、HTTPS、缓存、outbox、Rule/Skill 映射和更新；Codex 适配处理 Hook/MCP；Windows 薄层负责 Credential Manager、DACL、进程锁及安全命令转义。中文路径和默认非 UTF-8 控制台采用明确的 UTF-8 合同。

Server 保留原 Mac 的 schema-1 配置，新增按已登记设备隔离的路由、游标和回执；个人池按来源设备的有效 scope 合并相关召回。关闭、撤销、Secret/LocalOnly/no-share 和原预算设置继续有效。发布软件不批准新 Windows 设备或来源；真实邀请及来源启用仍按固定步骤对具体设备办理。

Windows 使用独立渠道 `releases/personal-https-windows-stable`。本阶段保留现有 Mac 4.0 运行渠道与不可变标签；Mac 与 Windows 共用代码和组件版本构建，Mac 0.5.0 构建产物先作为候选，不自动切换既有 Mac。源码 main、GitHub Release 标签及下载附件都不能替代安装时固定的渠道和信任锚。

验证包含 Mac 隔离回归、静态检查、Swift helper 自检，以及 Windows Server 2025 x64 runner 上的 Win32 凭据往返/CAS、DACL 拒绝、共享锁、中文 Hook、真实离线安装更新及断网恢复。Windows ARM64 桌面、用户真实配对输入、Codex UI 的 Hook trust 和实际模型采用未实测；没有读取真实聊天或添加付费模型探针。Windows 可选旧 memories 导入返回 unavailable，不影响未来 Hook 采集。WSL 和云端编排不在原生 Windows 本地 Hook 覆盖承诺内。

DeepSeek 在此版本仍只是 Server 提取 provider，DeepSeek Harness 接入留待独立开发任务。
