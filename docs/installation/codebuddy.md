# CodeBuddy 接入

0.8 旧布局请使用[保留记忆并重新连接的重装入口](reinstall.md)，普通 install 不会自动覆盖旧安装；旧未同步队列不会自动接续。
> Harness 功能对等整改基于 Client 0.8.9。发布状态以受信渠道为准；真实 IDE/CLI 的模型加载与用户验收保持暂停。

CodeBuddy IDE 和 CodeBuddy Code（CLI）共用现有 Agent Brain Client 与 Server。支持原生 Mac、Windows；首次只需 HTTPS Server 地址和本机隐藏输入的接入密钥，**不要求安装 Codex 应用**。已有 Client 则复用其原策略、设备身份、系统凭据与来源范围。

## 安装

从独立、干净的官方 `main` checkout 执行并记录源码 SHA。Mac 使用 arm64 或 x86_64 的原生引导；Windows 沿用既有 AMD64/ARM64 Python/Git 与系统凭据流程。

```sh
sh bootstrap/macos/install.sh --harness codebuddy --server 'https://your-server.example'
```

```powershell
& ./bootstrap/windows/install.ps1 -Harness codebuddy -Server 'https://your-server.example'
```

省略地址时沿用原安装器的本机输入／恢复。密钥不放进命令、聊天或环境变量。新设备把共享后端的原生登记放在私有 `shared-native` 目录，不创建用户 `.codex` 配置；原有安装保留原绑定。Server 冻结设备协议仍使用原 `codex` adapter 名称，这是兼容传输身份，不冒充 CodeBuddy 专属 Server grant，也不改变设备权限或信任锚。

已有 Client 时，安装核验共享登记并配置所选宿主。需要更新时使用共享入口的 `check-update` 和 `update`。终态 JSON 中的 `installation_stages.native_mcp` 报告 CodeBuddy 绑定，每个未完成阶段保留具体状态。`source_enabled` 表示当前采集授权，`model_loaded: "unknown"` 表示当前模型加载未被观察。

明确授权当前宿主的可共享个人原生记忆和既有接收目标后，请宿主运行 Agent Brain Onboarding（`agent_brain_server.memory_onboard`）。日常更新和同步恢复见[日常维护](maintenance.md)。

首次安装默认配置 IDE 和 CLI。只绑定一种时传 `--surface ide|cli` 或 Windows `-Surface ide|cli`。重复安装沿用已登记范围。切换范围前先执行下面的宿主解绑，再以所需范围重新安装。

## 实际配置与能力

| 项目 | 行为 |
| --- | --- |
| IDE MCP | 用户级 `~/.codebuddy/mcp.json` 的 `mcpServers.agent_brain_server`；路径依据本机官方 IDE 的 MCP dispatcher 实现核对 |
| CLI MCP | 按官方优先级使用首个已有 `.codebuddy/.mcp.json`、`.codebuddy/mcp.json`、`.codebuddy.json`；均不存在则创建首项 |
| Hooks | `.codebuddy/settings.json` 增加 SessionStart、UserPromptSubmit、Stop；保留其他 Hook 和权限 |
| 上下文 | IDE 使用 `~/.codebuddy/rules/agent-brain-client.md` 的自有说明块；CLI 使用 `~/.codebuddy/CODEBUDDY.md`；两端另有 Hook 的 `additionalContext` |
| 查询与写入 | 原验证后 MCP 启动链及完整工具集；CAS、幂等键、回执、队列与 Server 同原 Client |
| 自动召回 | UserPromptSubmit，复用原全局召回、来源证据检查、预算及离线标识 |
| 用户消息采集 | UserPromptSubmit 的实际 prompt；generation_id 可用时去重，不合并两次独立的相同提问 |
| CLI 助手采集 | Stop 提供时优先使用 last_assistant_message；缺失时仅读取本轮 prompt Hook 记录的 JSONL 游标之后的 assistant 文本 |
| IDE 助手采集 | Stop 的 generation_id 精确匹配当前会话 index.json 请求，只读取该请求引用的 assistant 消息文件 |
| 长回复与断网 | 按原事件协议分批，全部批次先写原 outbox，再联网；后续 Hook 逐条重试原事件，不生成替代请求 |

官方 Hooks 文档未承诺 Stop 最终正文；另外核对了官方 npm 包 `@tencent-ai/codebuddy-code` 2.180.0 的实际 Hook 输入转换代码，它会转发可用的 `last_assistant_message`，因此优先使用该字段，缺失再走原生正文读取。IDE 格式依据本机官方程序的 `HistoryStorage.getTranscriptPath`、`ConversationManager` 实现，JSONL 格式须匹配识别结构；均用合成文件验证，这不是实机模型验收。只接受 Hook 给出的 CodeBuddy 原生存储路径和安装后实际观察到的当前轮。不会扫描历史会话或读取旧消息正文；不采集工具输入输出、附件或推理内容。

文件缺失、尚未写完、轮次交叠、轮换、未知格式、路径越界或来源停止时保留证据并给出内容无关诊断；查询、正常 MCP 写入和可用的召回不因助手正文缺失而被关闭。IDE 落盘晚于 Stop 时，Hook 不阻塞宿主：先持久记录本轮已观察的 generation 与后台任务，再由共用后台投递器重读该轮完成证据；进程重启后可从原标记继续，后续 Hook 也可唤醒恢复。必须看到请求 `state=complete`，并核验消息身份及读取期间索引、正文未变；宿主对既有消息重写正文时可能保留旧 `isComplete=false`，不再以该字段单独拒收。取消、失败、授权撤销或范围变化不会继续后台采集。不会把未完成正文冒称为完整回复。原生读取每次至多 4 MiB；超过边界明确失败，不截断后声称完整。没有 generation_id 的 IDE index 格式无法精确定位助手回复，保留 prompt 采集与召回并报告缺口。

CodeBuddy 按正常宿主流程审核 MCP/Hooks。CLI 文档要求在 `/hooks` 面板审核外部变更；IDE 的项目级设置可能覆盖用户级设置，需要在实际项目检查。安装不会修改信任或权限配置。IDE 使用支持 MCP 的 Agent/Craft 模式；旧 Ask 模式不承诺 MCP。SDK 默认不加载文件配置，需要其调用方明确设置 settingSources。

云端 Rule/Skill 使用公共内容同步、所有权事务和回滚，原生路径为 `.codebuddy/rules` 与 `.codebuddy/skills`。Rule 的启停和触发条件保留；CLI 缺少 IDE 智能选取时使用可读索引引导按需读取，不扩大为始终加载全文。原生 Memory 来源支持 CLI Auto Memory、用户 `CODEBUDDY.md` 与已核对版本的 IDE 持久 Memory；必须经过公共审核才能准备云端导入。详见[职责、同一业务契约与版本证据](../development/harness-parity.md)。

项目命令可显式选择 `--harness codebuddy`，安装到项目 `.codebuddy/settings.local.json`，项目设置和队列与另一宿主隔离；项目链接、暂停、来源控制和原队列同步使用同一公共服务。只读链接不获得 transcript 采集权限。

## 状态、更新和卸载

`status` 读取本机登记。`probe` 使用既有认证执行只读协议核验。Mac：

```sh
sh bootstrap/macos/install.sh --action status --harness codebuddy
sh bootstrap/macos/install.sh --action probe --harness codebuddy
sh bootstrap/macos/install.sh --action unbind --harness codebuddy
```

Windows：

```powershell
& ./bootstrap/windows/install.ps1 -Action status -Harness codebuddy
& ./bootstrap/windows/install.ps1 -Action probe -Harness codebuddy
& ./bootstrap/windows/install.ps1 -Action unbind -Harness codebuddy
```

`unbind` 只移除已登记的 CodeBuddy 绑定、Hook 与说明块，保留共享 Client、其他宿主、凭据和待传事件。公共入口的 `uninstall` 是整机 Client 卸载。配置冲突或事务中断时核对自有前后像，并用相同入口续接。已运行的 MCP 由宿主正常关闭。

更新使用同一平台入口的 `check-update` 和 `update`。共享更新从已验证的活动包刷新原有 IDE／CLI 范围。观察新鲜 MCP 响应的 `client_runtime.reload_required`，给兼容 worker 完成切换的机会，仍需要时再使用宿主自己的 MCP 重载。

若从 0.8.29 更新时共享包已成功更新，随后旧安装器仍提示 CodeBuddy 绑定待复核，请从当前官方源码的统一入口再次检查和更新，沿用已有登记及 IDE／CLI 范围。

Git、局部 `P4CONFIG`／`.p4config`、`.uproject` 及 UE Engine 标记组合可用于识别工程，仍受原 scope 和 exclude 限制。首次设置会批准项目和普通目录的今后采集，普通目录使用已授权的个人记忆池。已有非项目目录清单继续限制采集与自动召回。已启用个人召回且未限制普通目录的旧全项目策略可在普通目录自动召回个人记忆。Server 支持后，普通更新会自动登记今后的普通目录采集，沿用原设备与暂停、排除及隔离设置。

## 官方依据

- [IDE MCP](https://www.codebuddy.cn/docs/ide/User-guide/MCP)
- [IDE Hooks](https://www.codebuddy.cn/docs/ide/Features/Hooks)
- [CLI MCP 优先级与 JSONC](https://www.codebuddy.cn/docs/cli/mcp)
- [CLI Hook 字段及输出](https://www.codebuddy.cn/docs/cli/hooks)
- [SDK 配置隔离](https://www.codebuddy.cn/docs/cli/sdk)

普通更新会同时刷新已登记的 CodeBuddy 冻结入口与原生 Hook，保留原 IDE/CLI 范围，不自动安装新宿主或恢复暂停。旧 IDE 说明块迁移到宿主实际读取的用户 rules 路径；首次生成的托管规则使用 `alwaysApply: true`，后续更新保留用户改过的 header（包括 `enabled: false`）；已有其他规则（包括禁用的旧 `agent-brain.md`）不变。新托管路径若已有非托管内容，或原 MCP/Hook 被用户更改，更新保留现场并要求复核，不覆盖或重新启用。安装成功不代表当前模型已加载新规则，仍需宿主实际会话验收。
