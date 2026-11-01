# 多平台开发与后续 Harness 接入

这次探索的主要重复来自原先把 Mac 路径、Swift helper、安装入口和单设备 Server 身份混进业务流程。Windows 与 Mac 的业务差异很小，但凭据、权限及进程锁不能靠换平台字符串解决。

共用业务核心：HTTPS 请求与回执、CAS/outbox、断网缓存、Rule/Skill 映射、安装事务及前后像、release manifest/健康回滚、来源语法和排除规则。共用 Codex 适配：Hook 输入、事件身份、来源发现、召回上下文、用户级 owned Hook 块与 MCP 协议。薄 OS 层：实际平台识别、路径语法、凭据 API、DACL/POSIX 权限、原子替换/刷盘、命令转义、共享锁与 worker 生命周期。

`tools/mac_first_install.py` 保留历史文件名和 Mac 入口兼容，内部流程供两平台共用；`install-mac.sh` 与 `install-windows.ps1` 只是依赖/入口包装。Windows 原生边界集中在 `scripts/brain_windows.py`，公共调用在 `brain_lock.py`。不为假想 OS 建框架，也不复制整套 OS×Harness 产品。

服务端个人池采用明确登记的设备路由与读取合并。每台设备独立 source/cursor/session/receipt/Core 目录，沿用同一 owner、profile_id 和累计预算。这样不会违反 Core 的单写入者 lease，也不需要搬迁现有 Mac 数据。跨设备 recall 仍使用真实请求者身份；provenance 按原来源设备的 scope、暂停和 enrollment 检查。主设备 schema-1 原目录保持兼容。新设备暂停不暂停 Mac；撤销设备后不继续从该设备召回。

后续同步开发/发布约定：

1. 每个业务改动在同一 PR 内跑 Mac 回归、Windows 原生边界和相同来源向量；新增纯业务模块不得依赖某个 OS API。
2. Client/Server 的独立协议副本以相同合成向量核对，不能通过跨仓 import 隐藏依赖。各仓均独立构建。
3. 每次同时构建 Mac/Windows manifest，保持统一组件版本；真实不同的 Swift/Win32 文件集合使用独立受信渠道，不能改旧安装的锚点或强换文件权限范围。
4. 修复需改凭据 backend/source pin、依赖或原生固定入口时，提供独立、可审查的迁移事务；活动包更新不冒充固定启动器更新。
5. 新 OS 的 CI 验证真实 API，不能只 mock 平台字符串。真实桌面、凭据提示和宿主 trust 不可测时列明边界，继续合成及静态开发，不运行真实聊天或付费探针。
6. 一项开发任务一个会话；跨会话交接记录仓库、分支、未提交文件、准确失败、运行命令和下一步，保留半成品。

后续 Harness 需交付：受支持官方事件来源及真实代码证据、稳定 request/event/session 身份、origin 与获准未来范围、取消/重复/中断语义、启动召回接口、正常安装/卸载/trust、断网队列与撤销合同。其适配器输出公共事件，不直接调用系统凭据 API或另造模型调度。缺某个生命周期事件时提供明确的固定补救步骤，不通过读取历史数据库伪装全自动。DeepSeek Harness 留待独立开发任务；当前 DeepSeek 提取 provider 不能计为 Harness 适配。
