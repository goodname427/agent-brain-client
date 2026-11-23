# Client 宿主适配约束

> 当前参考。覆盖 macOS／Windows 上的 Codex 和 CodeBuddy，核对基线 Client 0.8.36。

把仓库链接交给本地 Agent 时，从[统一安装引导](https://github.com/goodname427/agent-brain-client/blob/main/BOOTSTRAP.md)开始。首次安装由 [macOS](https://github.com/goodname427/agent-brain-client/blob/main/docs/installation/mac.md) 或 [Windows](https://github.com/goodname427/agent-brain-client/blob/main/docs/installation/windows.md) 的入口使用用户选定的 HTTPS 地址和本机密钥。[CodeBuddy IDE／CLI](https://github.com/goodname427/agent-brain-client/blob/main/docs/installation/codebuddy.md) 使用同一平台入口，显式选择宿主，首次绑定可指定界面。已有设备按[日常维护](https://github.com/goodname427/agent-brain-client/blob/main/docs/installation/maintenance.md)续接原身份和发行策略。

## 公共能力与宿主边界

- 两个宿主通过原生注册使用 `agent_brain_server` 的固定启动器和已验证 Worker。工具清单来自目标包的契约，受设备 grant、内容类型和运行能力约束。分别核对登记、Worker 的 `tools/list` 和实际宿主可见性。尚未观察模型加载时报告 `unknown`。
- 适配器负责宿主配置、事件转换、原生 Rule／Skill 映射和本机状态。Codex 与 CodeBuddy 有各自的配置优先级、Hook 审核和原生事件输入。凭据、文件权限、进程锁及路径差异由[平台层](https://github.com/goodname427/agent-brain-client/blob/main/docs/reference/multiplatform-development.md)处理。
- 公共业务遵循[协议](PROTOCOL.md)的设备身份、来源授权、CAS、原请求 ID 与回执。Server 持有内容写入与持久化边界。既有映射、暂停、原生工作流和待发送请求由相应的原地更新、修复或[重装事务](https://github.com/goodname427/agent-brain-client/blob/main/docs/installation/reinstall.md)保护。
- 正式包遵循既有 manifest、发布信任锚、文件范围、平台和能力校验。健康失败、中断与回滚沿用受管事务。新增宿主需要实际注册、事件、审批及恢复证据，兼容传输标识 `codex` 本身不授予宿主能力。

## 历史适配记录

旧 Mac Codex 的受限 SSH 迁移和 Topic MCP、签名组件候选、安装前后像与旧 Notify 的验收范围保存在[盘点基线的适配器全文](https://github.com/goodname427/agent-brain-client/blob/7136ee3319511ccb27df5a03f413d6b583eba1ed/docs/reference/ADAPTERS.md)。旧绑定或回执的历史语义可按原版本核对。
