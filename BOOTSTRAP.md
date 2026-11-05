# Client 安装／更新统一入口

用户把本仓库链接发给本地 Codex，即以此作为 Agent Brain 接入入口；无需额外附上安装提示。执行 Agent 按下列流程识别平台、检测并安装或升级。用户明确只想阅读、开发或执行其他工作时，以用户的目的为准。

先读 README.md 和 PROTOCOL.md。安装使用审核后固定的发布源、manifest、运行配置、设备范围和安装计划；保留原生工具工作流，不读取原生会话或数据库。不要运行旧混合仓的 brain.py/sync，也不要从旧仓拉取源码。

1. 识别实际执行环境。原生 Windows 读 [Windows 固定步骤](docs/installation/windows.md)，Mac 读 [Mac 安装、使用、卸载与再次安装](docs/installation/mac.md)。WSL 和云端编排不能按原生 Windows 本地 Hook 流程处理。
2. 依照平台文档取得独立、干净的官方 `main` checkout，并记录源码 SHA；不从用户旧目录执行入口。正常准备 Python 3.11+ 和 Git。先执行平台入口的 `status`，它只检查本地安装，不发邀请或读取设备凭据。
3. 已有经过核验的安装时，执行 `check-update`，成功检查后执行 `update`；不把重复 `install` 当作升级。复用现有策略、凭据、信任锚和队列，保留原有启用／关闭状态，不重新配对。Windows 命令如下；Mac 使用文档中的同名 action。

   ```powershell
   & ./tools/install-windows.ps1 -Action status
   & ./tools/install-windows.ps1 -Action check-update
   & ./tools/install-windows.ps1 -Action update
   ```

4. `status` 明确为 `client-not-installed` 时，运行平台安装入口。首次只提供用户明确选择的 **HTTPS Server 地址**，并让用户在本机隐藏输入中填写 Server 生成的长期接入密钥。Mac：`sh tools/install-mac.sh --server 'https://your-server.example'`；Windows：`& ./tools/install-windows.ps1 -Server 'https://your-server.example'`。省略地址时脚本会在本机询问地址。地址仅定位服务；密钥不得进入聊天、参数、环境变量或文件。Server 认证后自动返回适配平台的授权配置和发行信任锚，再完成发行校验、安全保存设备身份及本地安装事务。正常流程不要求 connection 文件、摘要、ready 回执或邀请码。服务尚未启用新协议时保留状态并报告，不拼接离线包绕过。中断重跑同一入口恢复；`client-uninstalled` 复用原身份重装，不申请新密钥。
5. 状态为 `review-required`、归属不明或有未完成事务时，保留原文件和回执，按对应文档的 status／repair 流程处理；不能将异常当作未安装而清理、重装或重置身份。
6. 安装／更新后先调用现有 MCP 工具，比较 `client_runtime` 的 worker 与活动包版本及 commit；一致无需重载，不一致才正常重载 `agent_brain_server`。无法观察则报告 unknown，不因更新动作本身要求重启。核实后继续既有 Onboard 流程。分别报告已验证活动包、成功检查的渠道版本和可观察的 worker 版本；未观察模型加载时报告 unknown。安装与更新默认开启本设备所有项目的今后会话采集，复用已有服务与设备身份，不补传历史；Hook trust 由宿主按正常流程处理。关闭、限制或不启用用户要求的功能前先确认。

验证源码时使用隔离 HOME 与 CODEX_HOME，测试只访问合成 localhost。构建不会证明模型已经加载 MCP、Keychain 提示通过或已部署。保留原生工具工作流；不读取原生会话或数据库。

Windows 的正常安装入口还会继续受信 launcher 交接、有限 Rule/Skill 映射与全局 Hook 引导。
逐项查看 `installation_stages`；MCP 可列工具不等于映射或 Hook 已生效。
安装和更新自动核对现有服务的个人池并登记全项目来源，不要求 profile ID、批准摘要或 SSH 操作。
失败重跑同一入口恢复，保留原配置、身份和队列；不扫描历史。正常重载和新会话加载由宿主完成。
操作说明见 [Windows installation stages](docs/installation/windows-stages.md)。
