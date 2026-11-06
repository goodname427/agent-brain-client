# Windows Codex 安装、升级与未来记忆

> 状态：当前维护。适用：Windows 当前安装入口；0.5.9 基线。文档核对日期：2026-11-03；验收范围见 [0.5 交付状态](../releases/0.5-status.md)。

可以只把本仓库链接发给 Windows 上的本地 Codex；入口约定见 [统一引导](../../BOOTSTRAP.md)。执行 Agent 先从干净官方 main 运行 `& ./bootstrap/windows/install.ps1 -Action status`。已有核验安装时使用 `-Action check-update`、`-Action update`，保留现有身份与策略；明确未安装时才继续下述首次安装。状态异常或事务未完成时保留材料并按 status／repair 恢复，不重新发码或重置。

适用原生 Windows x64/ARM64、本地编排的 Codex。WSL 是另一套 Linux 环境；Work Cloud 编排不执行本地 command Hook，不能据此承诺云会话覆盖。安装不读取原生 session/history。用户已确认本轮 Windows 安装与验收通过；公开记录没有列出设备架构和逐项结果，因此不推定所有架构、Hook trust、模型加载或异常恢复场景均通过。新设备仍需正常操作和逐项核对，参见 [交付状态](../releases/0.5-status.md)。

使用当前用户的普通 PowerShell。准备 Python 3.11+、Git 和 Codex；没有依赖时可从它们的官方安装器安装，或用已安装的 winget 安装 Python.Python.3.12 和 Git.Git。安装完成后重新打开终端。无需管理员运行，不更改执行策略、沙箱或 Hook 信任设置。

首次安装只需要服务发行者提供的 HTTPS 地址和长期接入密钥。密钥仅在本机Windows 原生凭据窗口的隐藏密码框中输入，保存在用户 Credential Manager；不能发送到聊天或放入命令行、环境变量、配置文件。Windows 更新继续使用独立的 `releases/personal-https-windows-stable` 渠道。

```powershell
$checkout = Join-Path $env:TEMP ('agent-brain-client-' + [guid]::NewGuid())
git -c core.autocrlf=false -c core.eol=lf clone --depth 1 --single-branch --branch main --no-tags https://github.com/goodname427/agent-brain-client.git $checkout
Set-Location $checkout
& ./bootstrap/windows/install.ps1
```

脚本在本机询问 HTTPS Server 地址（也可用非秘密参数 `-Server 'https://your-server.example'` 指定），再用 Windows 原生凭据窗口请求隐藏输入密钥。认证后自动获取该 Windows 平台授权及发布元数据，核验发行包、保存设备身份并完成本地 MCP 安装。不要求 connection.json、文件摘要、ready.json 或邀请码。中断重跑相同入口，复用密钥、nonce、安装计划和队列。错误密钥可重新输入；网络或证书错误保留原记录，不能跳过 HTTPS 验证。只有本地事务与只读协议检查都成功才报告 installed:true。

0.5.3 的入口会自动跳过 Windows Store 执行别名，查找可固定摘要的用户 Python／Python launcher／注册解释器；不需要手改 PATH。Python 3.11 仍受支持，Windows 架构不再依赖被最小运行环境移除的变量。Git 换行由仓库和独立 checkout 固定为 LF，真实字节变化仍拒绝。

已在 0.5.0 运行包完成设备登记、但安装检查失败且尚未生成原生安装回执时，取得新的官方 main 后正常重跑安装入口即可复用原接入密钥与设备身份。入口只从原受信渠道获取兼容包，保存旧 bootstrap 与计划，按精确原生前像刷新未执行计划，再运行原安装事务。已开始或已完成的原生事务走原恢复，不能套用此迁移。预登记 CRLF 元数据仅在无设备身份、无原生事务且精确匹配已知换行差异时自动迁移，原始摘要保存在恢复记录中。不要移动目录、重写摘要或重新生成密钥。

安装成功后正常重载 Codex 的 `agent_brain_server` MCP，并按既有 Onboard 流程核对获授权的 Memory/Rule/Skill。模型加载不可观测时仍报告 unknown。完整信任与恢复说明见 [地址与密钥安装](address-key-install.md)。

安装和更新默认开启本设备所有项目的今后会话采集，复用已有 HTTPS 设备身份。先读取已认证服务的个人池，再登记和开启，不再要求重复批准摘要、手填 profile ID 或 SSH 管理操作。已有排除目录、项目隔离、非项目设置、数据与队列保留；不会补传历史。

Client 使用现有设备 HTTPS 凭据将无凭据来源元数据登记到同一个人记忆池，读回核对设备和范围后才激活。无需把设备包交给 Server operator，不生成新长期凭据，不扫描历史。Server 必须包含 `authenticated-device-v1` 来源登记接口；未确认结果保留原采集配置与来源包，重跑同一安装或更新入口恢复。

需要单独修复来源登记时，可使用以下入口：

```powershell
& ./bin/memory.ps1 global-status --check-server
# 正常安装或更新自动完成全项目接入；中断时重跑同一入口即可。
& ./bootstrap/windows/install.ps1 -Action update
# 只修复来源登记也可以使用：
& ./bin/memory.ps1 global-configure --all-projects
& ./bin/memory.ps1 global-status --check-server
```

`global-status --check-server` 分别报告 `source_registered`、`recall_authorized` 和采集停止状态。`source_registered=false` 不妨碍已授权的个人池召回；空范围仍不允许采集。当前 checkout 与 Hook pin 不一致时先走正常 update，不能手改摘要。

全项目范围由 `all_projects: true` 表示，不需要列举盘符或项目根目录；新盘和新项目同样适用。保留已有的排除目录与项目隔离规则，不额外开启非项目对话。

`global-resume` 只恢复原本已批准的来源，不补齐空范围或 Server 登记。空范围、缺少批准或未完成激活时返回 `global-memory-activation-required` 和非零退出码；此路径不调用 Server 或访问凭据。已批准的暂停来源可显式恢复，只有 Server 明确返回 `schema:1`、`stopped:false` 才撤掉本地停止标记。错误、停止状态或缺失确认返回 `global-memory-resume-unconfirmed`，保留标记和错误代码。旧的逐项目采集独立恢复时标为 `global-memory-legacy-resumed`，不声称用户级扩大范围已启用。

在 Codex `/hooks` 正常审阅并信任新增定义，然后新建本地会话。用户级 Hook 覆盖获准目录中的项目、子目录、worktree 和非项目会话。SessionStart/UserPromptSubmit 按当前问题召回；Stop 将未来事件写入原 outbox，Server 常驻消费者沿用 DeepSeek 提取及 GitHub 备份。来源、请求身份、冲突和恢复账本保留；跨设备内容只作相关参考，不自动变成全局 Rule。Secret/LocalOnly/no-share 继续排除。

```powershell
& ./bootstrap/windows/install.ps1 -Action status
& ./bootstrap/windows/install.ps1 -Action check-update
& ./bootstrap/windows/install.ps1 -Action update
& ./bin/memory.ps1 global-stop
& ./bin/memory.ps1 global-resume
& ./bin/memory.ps1 global-sync
& ./bin/memory.ps1 global-uninstall
& ./bootstrap/windows/install.ps1 -Action uninstall
```

升级复用现有 policy、凭据、信任锚、缓存和幂等队列；不重新配对。不确定/冲突回执保留，断网读明确标为旧缓存，恢复后重放原 request_id。卸载前正常停止该 MCP worker，再移除 owned Hook；普通卸载不清凭据和记忆。显示 `review-required` 时保留原材料，用 status/repair 处理未完成事务。安装目录或 Python/Git 被移动/升级而摘要变化时，需要精确依赖迁移，不能替换摘要掩盖漂移。

正常 update 同时交接旧固定启动器，安装或刷新全局 Hook，并自动登记及开启全项目今后采集，包含旧设备和非交互更新。现有数据、队列、排除目录、隔离规则和设备身份保留。按 Codex 正常提示审核更新后的 Hook，再重新加载 Agent Brain。check-update 仅报告候选和 Hook 待刷新状态。Hook 刷新未确认时即使活动包已更新也退出非零；重试同一 update 入口恢复事务，不重复批准来源或配对。

Windows 凭据使用用户 Credential Manager；私有状态检查 DACL、owner 和 reparse point，锁使用 LockFileEx。没有轮询聊天目录的本地常驻采集器；后台提取消费在 Server，MCP worker 由 Codex 管理生命周期。0.5.9 已包含 Mac／Windows 的有界原生 `CODEX_HOME/memories` 导入，具体边界见 [现有接口说明](../reference/memory-behavior.md)；该能力不包含 `memories_v2`、SQLite 或聊天历史，今后 Hook 采集不要求重复来源批准。未实现 DeepSeek Harness；DeepSeek 当前只是提取 provider。

协议依据：[Codex Hooks](https://learn.chatgpt.com/docs/hooks)、[Windows Sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)、[Win32 Credential](https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw)、[LockFileEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex)。
