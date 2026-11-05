# CodeBuddy 接入

> Client 0.7 适配源码。源码合入不等于已发布；真实 IDE/CLI 的模型加载与用户验收另行核对。

CodeBuddy IDE 和 CodeBuddy Code（CLI）共用现有 Agent Brain Client 与 Server。支持原生 Mac、Windows；首次只需 HTTPS Server 地址和本机隐藏输入的接入密钥，**不要求安装 Codex 应用**。已有 Client 则复用其原策略、设备身份、系统凭据与来源范围。

## 安装

从独立、干净的官方 `main` checkout 执行并记录源码 SHA。Mac 沿用现有安装器的 arm64 首次引导；Windows 沿用既有 AMD64/ARM64 Python/Git 与系统凭据流程。

```sh
sh tools/install-codebuddy.sh --server 'https://your-server.example'
```

```powershell
& ./tools/install-codebuddy.ps1 -Server 'https://your-server.example'
```

省略地址时沿用原安装器的本机输入／恢复。密钥不放进命令、聊天或环境变量。新设备把共享后端的原生登记放在私有 `shared-native` 目录，不创建用户 `.codex` 配置；原有安装保留原绑定。Server 冻结设备协议仍使用原 `codex` adapter 名称，这是兼容传输身份，不冒充 CodeBuddy 专属 Server grant，也不改变设备权限或信任锚。

已有 Client 时，入口先沿原受信渠道执行 check-update／update，刷新 0.7 共用运行时；重复原 install 本身不能代替升级。安装再验证共享 Client 的 MCP initialize、工具目录与只读认证，然后事务安装 CodeBuddy 配置。`installed:true` 表示配置与协议核验通过；`source_enabled` 单独反映现有全局采集授权，`model_loaded:false` 表示未运行模型验收。来源状态异常应沿原安装器恢复；不能替换凭据或绕过 Server 拒绝。旧渠道尚未提供共用模块时保留原安装，等待统一 0.7 发行，不冒称自动采集已就绪。

默认配置 IDE 与 CLI。需要单独绑定其中一种时，在共享 Client 安装完成后执行 `python3 -I -B tools/codebuddy.py install --surface ide` 或 `--surface cli`。已安装后切换范围应先卸载原 CodeBuddy 绑定，保留共享后端。

## 实际配置与能力

| 项目 | 行为 |
| --- | --- |
| IDE MCP | 用户级 `~/.codebuddy/mcp.json` 的 `mcpServers.agent_brain_server`；路径依据本机官方 IDE 的 MCP dispatcher 实现核对 |
| CLI MCP | 按官方优先级使用首个已有 `.codebuddy/.mcp.json`、`.codebuddy/mcp.json`、`.codebuddy.json`；均不存在则创建首项 |
| Hooks | `.codebuddy/settings.json` 增加 SessionStart、UserPromptSubmit、Stop；保留其他 Hook 和权限 |
| 上下文 | `.codebuddy/CODEBUDDY.md` 的自有说明块，以及 Hook 的 `additionalContext` |
| 查询与写入 | 原验证后 MCP 启动链及完整工具集；CAS、幂等键、回执、队列与 Server 同原 Client |
| 自动召回 | SessionStart 和 UserPromptSubmit，复用原全局召回、来源证据检查、预算及离线标识 |
| 用户消息采集 | UserPromptSubmit 的实际 prompt；generation_id 可用时去重，不合并两次独立的相同提问 |
| CLI 助手采集 | Stop 提供时优先使用 last_assistant_message；缺失时仅读取本轮 prompt Hook 记录的 JSONL 游标之后的 assistant 文本 |
| IDE 助手采集 | Stop 的 generation_id 精确匹配当前会话 index.json 请求，只读取该请求引用的 assistant 消息文件 |
| 长回复与断网 | 按原事件协议分批，全部批次先写原 outbox，再联网；后续 Hook 逐条重试原事件，不生成替代请求 |

官方 Hooks 文档未承诺 Stop 最终正文；另外核对了官方 npm 包 `@tencent-ai/codebuddy-code` 2.180.0 的实际 Hook 输入转换代码，它会转发可用的 `last_assistant_message`，因此优先使用该字段，缺失再走原生正文读取。IDE 格式依据本机官方程序的 `HistoryStorage.getTranscriptPath`、`ConversationManager` 实现，JSONL 格式须匹配识别结构；均用合成文件验证，这不是实机模型验收。只接受 Hook 给出的 CodeBuddy 原生存储路径和安装后实际观察到的当前轮。不会扫描历史会话或读取旧消息正文；不采集工具输入输出、附件或推理内容。

文件缺失、尚未写完、轮次交叠、轮换、未知格式、路径越界或来源停止时保留证据并给出内容无关诊断；查询、正常 MCP 写入和可用的召回不因助手正文缺失而被关闭。IDE 落盘晚于 Stop 时，后续 Hook 会重读先前实际观察到的该 generation，不把未完成正文冒称为完整回复。原生读取每次至多 4 MiB；超过边界明确失败，不截断后声称完整。没有 generation_id 的 IDE index 格式无法精确定位助手回复，保留 prompt 采集与召回并报告缺口。

CodeBuddy 按正常宿主流程审核 MCP/Hooks。CLI 文档要求在 `/hooks` 面板审核外部变更；IDE 的项目级设置可能覆盖用户级设置，需要在实际项目检查。安装不会修改信任或权限配置。IDE 使用支持 MCP 的 Agent/Craft 模式；旧 Ask 模式不承诺 MCP。SDK 默认不加载文件配置，需要其调用方明确设置 settingSources。

云端 Rule/Skill 的内容操作仍可通过完整 MCP 使用；本适配不把现有 Codex 原生 Rule/Skill 文件映射冒称为 CodeBuddy 原生加载。

## 状态、更新和卸载

用安装器准备好的 Python 3.11+ 运行（Windows 也可使用现有 Python resolver）：

```sh
python3 -I -B tools/codebuddy.py status
python3 -I -B tools/codebuddy.py probe
python3 -I -B tools/codebuddy.py uninstall
```

`uninstall` 只移除自有 CodeBuddy 绑定、Hook 与说明块，保留其他配置、共享后端、系统凭据和所有待传事件。配置漂移时不覆盖；中断事务按原 host-file 前后像恢复。已启动的 MCP 需按宿主正常流程结束；不终止用户进程。

更新先沿现有共享 Client 的 `check-update`／`update` 流程，再从同一新版官方 checkout 运行 CodeBuddy `install` 刷新适配器快照。也可运行 Mac `sh tools/install-codebuddy.sh --action update` 或 Windows `& ./tools/install-codebuddy.ps1 -Action update`。保持原渠道和来源设置；更新适配器不是重新配对。只在 worker 与活动版本实际不同时正常重载对应 MCP。

## 官方依据

- [IDE MCP](https://www.codebuddy.cn/docs/ide/User-guide/MCP)
- [IDE Hooks](https://www.codebuddy.cn/docs/ide/Features/Hooks)
- [CLI MCP 优先级与 JSONC](https://www.codebuddy.cn/docs/cli/mcp)
- [CLI Hook 字段及输出](https://www.codebuddy.cn/docs/cli/hooks)
- [SDK 配置隔离](https://www.codebuddy.cn/docs/cli/sdk)
