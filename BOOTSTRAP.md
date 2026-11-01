# Client 安装／更新统一入口

用户把本仓库链接发给本地 Codex，即以此作为 Agent Brain 接入入口；无需额外附上安装提示。执行 Agent 按下列流程识别平台、检测并安装或升级。用户明确只想阅读、开发或执行其他工作时，以用户的目的为准。

先读 README.md 和 PROTOCOL.md。安装使用审核后固定的发布源、manifest、运行配置、设备范围和安装计划；保留原生工具工作流，不读取原生会话或数据库。不要运行旧混合仓的 brain.py/sync，也不要从旧仓拉取源码。

1. 识别实际执行环境。原生 Windows 读 [Windows 固定步骤](docs/FIRST-INSTALL-WINDOWS.md)，Mac 读 [Mac 安装、使用、卸载与再次安装](docs/FIRST-INSTALL-MAC.md)。WSL 和云端编排不能按原生 Windows 本地 Hook 流程处理。
2. 依照平台文档取得独立、干净的官方 `main` checkout，并记录源码 SHA；不从用户旧目录执行入口。正常准备 Python 3.11+ 和 Git。先执行平台入口的 `status`，它只检查本地安装，不发邀请或读取设备凭据。
3. 已有经过核验的安装时，执行 `check-update`，成功检查后执行 `update`；不把重复 `install` 当作升级。复用现有策略、凭据、信任锚和队列，保留原有启用／关闭状态，不重新配对。Windows 命令如下；Mac 使用文档中的同名 action。

   ```powershell
   & ./tools/install-windows.ps1 -Action status
   & ./tools/install-windows.ps1 -Action check-update
   & ./tools/install-windows.ps1 -Action update
   ```

4. `status` 明确为 `client-not-installed` 或 `client-uninstalled` 时，进入平台文档的首次安装流程。Windows 可先运行 `& ./tools/install-windows.ps1`；没有可信连接资料时，入口返回 `first-install-connection-required`，按提示取得发行者交付的该平台 connection.json 及摘要后继续。不使用示例连接，不猜地址、摘要或设备身份；邀请在 ready.json 生成后走安全输入，不放进聊天、命令行或环境变量。中断后复用原材料和身份恢复。
5. 状态为 `review-required`、归属不明或有未完成事务时，保留原文件和回执，按对应文档的 status／repair 流程处理；不能将异常当作未安装而清理、重装或重置身份。
6. 安装／更新后正常重载 `agent_brain_server`，继续既有 Onboard 流程。分别报告已验证活动包、成功检查的渠道版本和可观察的 worker 版本；未观察模型加载时报告 unknown。首次新设备权限、未来采集目录及 Hook trust 仍需正常授权，仓库链接本身不扩大这些范围。

验证源码时使用隔离 HOME 与 CODEX_HOME，测试只访问合成 localhost。构建不会证明模型已经加载 MCP、Keychain 提示通过或已部署。保留原生工具工作流；不读取原生会话或数据库。
