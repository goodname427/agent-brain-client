# Windows Codex 安装、升级与未来记忆

可以只把本仓库链接发给 Windows 上的本地 Codex；入口约定见 [统一引导](../BOOTSTRAP.md)。执行 Agent 先从干净官方 main 运行 `& ./tools/install-windows.ps1 -Action status`。已有核验安装时使用 `-Action check-update`、`-Action update`，保留现有身份与策略；明确未安装时才继续下述首次安装。状态异常或事务未完成时保留材料并按 status／repair 恢复，不重新发码或重置。

适用原生 Windows x64/ARM64、本地编排的 Codex。WSL 是另一套 Linux 环境；Work Cloud 编排不执行本地 command Hook，不能据此承诺云会话覆盖。安装不读取原生 session/history。真实 Windows 桌面、用户隐藏密钥输入与 Codex Hook trust 尚需该设备正常操作；Windows runner 的合成检查不等于这些实机体验已验收。

使用当前用户的普通 PowerShell。准备 Python 3.11+、Git 和 Codex；没有依赖时可从它们的官方安装器安装，或用已安装的 winget 安装 Python.Python.3.12 和 Git.Git。安装完成后重新打开终端。无需管理员运行，不更改执行策略、沙箱或 Hook 信任设置。

首次安装只需要服务发行者提供的 HTTPS 地址和长期接入密钥。密钥仅在本机隐藏的控制台提示中输入，保存在用户 Credential Manager；不能发送到聊天或放入命令行、环境变量、配置文件。Windows 更新继续使用独立的 `releases/personal-https-windows-stable` 渠道。

```powershell
$checkout = Join-Path $env:TEMP ('agent-brain-client-' + [guid]::NewGuid())
git clone --depth 1 --single-branch --branch main --no-tags https://github.com/goodname427/agent-brain-client.git $checkout
Set-Location $checkout
& ./tools/install-windows.ps1
```

脚本在本机询问 HTTPS Server 地址（也可用非秘密参数 `-Server 'https://your-server.example'` 指定），再请求隐藏输入密钥。认证后自动获取该 Windows 平台授权及发布元数据，核验发行包、保存设备身份并完成本地 MCP 安装。不要求 connection.json、文件摘要、ready.json 或邀请码。中断重跑相同入口，复用密钥、nonce、安装计划和队列。错误密钥可重新输入；网络或证书错误保留原记录，不能跳过 HTTPS 验证。只有本地事务与只读协议检查都成功才报告 installed:true。

安装成功后正常重载 Codex 的 `agent_brain_server` MCP，并按既有 Onboard 流程核对获授权的 Memory/Rule/Skill。模型加载不可观测时仍报告 unknown。完整信任与恢复说明见 [地址与密钥安装](ADDRESS-KEY-INSTALL.md)。

未来自动采集需单独确认来源。以下仅为用户替换后的固定步骤，不把示例目录当作已获准范围：

```powershell
& ./tools/memory-windows.ps1 global-install --profile-id '<既有个人池32位ID>'
& ./tools/memory-windows.ps1 global-configure --include-root 'C:/Work' --exclude-root 'C:/Work/Private' --non-project-root 'C:/Notes'
# 审阅上一步显示的精确 scope 和摘要；确认后生成无凭据来源包：
& ./tools/memory-windows.ps1 global-configure --approve-scope '<上一步摘要>' --packet-output 'C:/delivery/windows-source.json'
```

由既有 Server operator 将该包交给 `tools/manage_global_memory.py prepare`，审查后 `apply`。它验证独立的 active enrollment、平台路径、同一 owner、同一个人池和接收地址，保留原 Mac 策略及账本。此操作是具体设备来源授权，普通代码开发不代替它。确认部署回执后，在 Windows 运行：

```powershell
& ./tools/memory-windows.ps1 global-configure --activate-packet 'C:/delivery/windows-source.json'
& ./tools/memory-windows.ps1 global-status
```

在 Codex `/hooks` 正常审阅并信任新增定义，然后新建本地会话。用户级 Hook 覆盖获准目录中的项目、子目录、worktree 和非项目会话。SessionStart/UserPromptSubmit 按当前问题召回；Stop 将未来事件写入原 outbox，Server 常驻消费者沿用 DeepSeek 提取及 GitHub 备份。来源、请求身份、冲突和恢复账本保留；跨设备内容只作相关参考，不自动变成全局 Rule。Secret/LocalOnly/no-share 继续排除。

```powershell
& ./tools/install-windows.ps1 -Action status
& ./tools/install-windows.ps1 -Action check-update
& ./tools/install-windows.ps1 -Action update
& ./tools/memory-windows.ps1 global-stop
& ./tools/memory-windows.ps1 global-resume
& ./tools/memory-windows.ps1 global-sync
& ./tools/memory-windows.ps1 global-uninstall
& ./tools/install-windows.ps1 -Action uninstall
```

升级复用现有 policy、凭据、信任锚、缓存和幂等队列；不重新配对。不确定/冲突回执保留，断网读明确标为旧缓存，恢复后重放原 request_id。卸载前正常停止该 MCP worker，再移除 owned Hook；普通卸载不清凭据和记忆。显示 `review-required` 时保留原材料，用 status/repair 处理未完成事务。安装目录或 Python/Git 被移动/升级而摘要变化时，需要精确依赖迁移，不能替换摘要掩盖漂移。

Windows 凭据使用用户 Credential Manager；私有状态检查 DACL、owner 和 reparse point，锁使用 LockFileEx。没有轮询聊天目录的本地常驻采集器；后台提取消费在 Server，MCP worker 由 Codex 管理生命周期。可选的旧 Codex memories 导入目前没有 Windows reader，返回 unavailable，未来 Hook 采集不依赖它。未实现 DeepSeek Harness；DeepSeek 当前只是提取 provider。

协议依据：[Codex Hooks](https://learn.chatgpt.com/docs/hooks)、[Windows Sandbox](https://learn.chatgpt.com/docs/windows/windows-sandbox)、[Win32 Credential](https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw)、[LockFileEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex)。
