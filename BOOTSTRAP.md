# Client 安装／更新统一入口

Codex 和 [CodeBuddy IDE／CLI](https://github.com/goodname427/agent-brain-client/blob/main/docs/installation/codebuddy.md) 共用同一平台入口。CodeBuddy 在 Mac 传 `--harness codebuddy`，在 Windows 传 `-Harness codebuddy`。首次绑定可选 IDE、CLI 或两者，已有安装沿用登记范围。新设备可以只安装 CodeBuddy。自动采集使用未来实际观察到的 Hook 与该轮原生正文。

用户把本仓库链接发给本地 Codex 或 CodeBuddy，即以此作为 Agent Brain 接入入口；无需额外附上安装提示。执行 Agent 按下列流程识别实际平台和宿主，再检测、安装或升级。用户明确只想阅读、开发或执行其他工作时，以用户的目的为准。

先读 [README](README.md) 和 [协议](https://github.com/goodname427/agent-brain-client/blob/main/docs/reference/PROTOCOL.md)。安装使用审核后固定的发布源、manifest、运行配置、设备范围和安装计划；保留原生工具工作流，不读取原生会话或数据库。不要运行旧混合仓的 brain.py/sync，也不要从旧仓拉取源码。已有可用安装且用户只要求 Onboarding 时，直接进入下方 [Onboarding](#onboarding)。

1. 识别实际执行环境。原生 Windows 读 [Windows 固定步骤](https://github.com/goodname427/agent-brain-client/blob/main/docs/installation/windows.md)，Mac 读 [Mac 安装、使用、卸载与再次安装](https://github.com/goodname427/agent-brain-client/blob/main/docs/installation/mac.md)。WSL 和云端编排不能按原生 Windows 本地 Hook 流程处理。
2. 依照平台文档取得独立、干净的官方 `main` checkout，并记录源码 SHA；不从用户旧目录执行入口。正常准备 Python 3.11+ 和 Git。先执行平台入口的 `status`，它只检查本地安装，不发邀请或读取设备凭据。
3. 0.8 结构切换使用明确的[全新连接式重装](https://github.com/goodname427/agent-brain-client/blob/main/docs/installation/reinstall.md)：先运行 `reinstall-check` 检查精确租约，按报告只断开受影响的 Agent Brain MCP 服务并等待活动 Hook／事务完成（不要求关闭 IDE 或全部 Client）；旧 worker 无法单独退出时停止并报告，不强杀或删锁。随后 Mac 执行 `sh bootstrap/macos/install.sh --action reinstall --server 'https://your-server.example'`，Windows 执行 `& ./bootstrap/windows/install.ps1 -Action reinstall -Server 'https://your-server.example'`；CodeBuddy 使用同一平台入口，并传入对应的宿主选项。按本机提示确认归档，在隐藏输入中重新填写原 Server/账号的接入密钥。旧文件和未同步队列保留，旧身份和队列不会自动接续；保留原 MCP／采集暂停、停止标记和原来源范围，不自动恢复或扩展；不删除目录、不改 pin，也不把普通 install 当作覆盖。普通同布局版本已有经过核验的安装时，执行 `check-update`，成功检查后执行 `update`；不把重复 `install` 当作升级。复用现有策略、凭据、信任锚和队列，保留原有启用／关闭状态，不重新配对。Windows 命令如下；Mac 使用文档中的同名 action。

   ```powershell
   & ./bootstrap/windows/install.ps1 -Action status
   & ./bootstrap/windows/install.ps1 -Action check-update
   & ./bootstrap/windows/install.ps1 -Action update
   ```

4. `status` 明确为 `client-not-installed` 时，运行平台安装入口。首次只提供用户明确选择的 **HTTPS Server 地址**，并让用户在本机隐藏输入中填写 Server 生成的长期接入密钥。Mac：`sh bootstrap/macos/install.sh --server 'https://your-server.example'`；Windows：`& ./bootstrap/windows/install.ps1 -Server 'https://your-server.example'`。省略地址时脚本会在本机询问地址。地址仅定位服务；密钥内容不得进入聊天、参数或环境变量。用户授权后可用 `--access-key-file`（Windows `-AccessKeyFile`）传入本机 Key 文件路径，复用同一认证流程，不必手动输入。Server 认证后自动返回适配平台的授权配置和发行信任锚，再完成发行校验、安全保存设备身份及本地安装事务。正常流程不要求 connection 文件、摘要、ready 回执或邀请码。服务尚未启用新协议时保留状态并报告，不拼接离线包绕过。中断重跑同一入口恢复；`client-uninstalled` 复用原身份重装，不申请新密钥。
5. 状态为 `review-required`、归属不明或有未完成事务时，保留原文件和回执，按对应文档的 status／repair 流程处理；不能将异常当作未安装而清理、重装或重置身份。
6. 安装／更新后先调用现有 MCP 工具，检查新鲜响应中的 `client_runtime.reload_required`、worker 与活动包标识。兼容 worker 会等在途请求结束后在原连接切换。复查后仍需重载，或宿主仍看不到所需新工具时，只用正常流程重载 `agent_brain_server`。无法观察时报告 unknown。核实后按下方 [Onboarding](#onboarding)完成接入交接。分别报告已验证活动包、成功检查的渠道版本和可观察的 worker 版本；未观察模型加载时报告 unknown。首次安装默认开启本设备项目和普通目录的今后会话采集与个人记忆召回。更新复用已有服务与设备身份。已启用的旧全项目策略如果没有普通目录限制且允许个人召回，会在 Server 支持后自动纳入今后的普通目录采集。其他明确范围和暂停继续保留。全程不补传历史；Hook trust 由宿主按正常流程处理。关闭、限制或不启用用户要求的功能前先确认。

验证源码时使用隔离 HOME 与 CODEX_HOME，测试只访问合成 localhost。构建不会证明模型已经加载 MCP、Keychain 提示通过或已部署。保留原生工具工作流；不读取原生会话或数据库。

Windows 的正常安装入口还会继续受信 launcher 交接、有限 Rule/Skill 映射与全局 Hook 引导。
逐项查看 `installation_stages`；MCP 可列工具不等于映射或 Hook 已生效。
安装和更新自动核对现有服务的个人池并登记全项目来源，不要求 profile ID、批准摘要或 SSH 操作。
失败重跑同一入口恢复，保留原配置、身份和队列；不扫描历史。正常重载和新会话加载由宿主完成。
操作说明见 [Windows installation stages](https://github.com/goodname427/agent-brain-client/blob/main/docs/installation/windows-stages.md)。

## Onboarding

从此公开引导开始。即使 Agent Brain 里还没有记忆，或宿主没有加载任何 Agent Brain Skill，也可使用已安装的 `agent_brain_server` MCP 完成首次使用。`onboard_check.status:coverage-check-required` 表示进入本节。安装和重装回执中的 `source_scan_performed:false`、`data_transferred:false` 记录截至交接时的实际状态。安装与未来 Hook 自动采集的授权不等于历史导入授权。

1. **核对接入与范围。** 用新鲜的 `content_catalog({"kinds":["memory"]})` 核对目标库和可用 worker；Rule／Skill 仅在相应授权范围内加入 `kinds`。用 `next_cursor` 遍历目标目录，用 `content_read` 读取一个已知条目（如有）。空目录也是有效读结果；旧缓存不能证明在线接入。若有获准导入的原生持久记忆，按实际宿主与用户授权记录本轮要检查的来源及范围。仅接入或未授权导入时，完成只读验证，并报告来源未检查。日常按主题查找用 `memory_query`。
2. **盘点原生来源。** 在已获准的宿主上调用 `content_sources_inventory`，用 `next_cursor` 读完全部页，保留来源 ID、指纹及状态。先收齐页面再做逐条审阅；游标失效时从第一页重列。`content_catalog` 只列 Agent Brain 目标记录。Codex 的导入范围为已绑定的 `CODEX_HOME/memories`；CodeBuddy 按当前实际工具及回执核对已支持的持久来源。工具不可用、目录缺失或超限时保留缺口；不扫描聊天历史、数据库、工作区私有目录或其他未授权来源。详见[原生来源边界](https://github.com/goodname427/agent-brain-client/blob/main/docs/reference/memory-behavior.md)与 [CodeBuddy 接入](https://github.com/goodname427/agent-brain-client/blob/main/docs/installation/codebuddy.md)。
3. **逐条读取、审阅和比对。** 用 `content_sources_read` 读取确切的 `source_id` 和 `source_fingerprint`，再用 `content_sources_review` 记录判断。排除秘密、LocalOnly、客户或雇主机密及原始对话；无法安全跨项目泛化、来源不确定或超出授权范围时暂缓。指纹变化后重新盘点和审阅。保留安全可共享的独有事实、普通经历、纠正和短期进度，不按长期重要性或召回条数筛减。比对目标目录并用新鲜的 `content_read` 取目标正文及 revision。等价条目计为重复，无需写入；保留双方独有事实和来源，发现矛盾时先审阅，不覆盖已有记录或恢复已删除的记忆。
4. **暂存并执行获准的 CAS 写入。** 完成去重、合并及纠正后，仅将已标为 `shareable` 的最终 Memory 候选用 `content_sources_stage` 暂存。暂存只创建本地计划并返回 `source_key`；同一指纹只能重复提交相同候选。显式调用 `content_put`，提交同一候选、稳定的 `request_id`、本次目标读出的 `expected_revision`（新条目使用读回的 `missing`），并在 `reconciles.source_keys` 填入该 `source_key`。明确获准且已经提供的 Rule／Skill 按普通 [CAS 与启停合同](https://github.com/goodname427/agent-brain-client/blob/main/docs/reference/rule-skill-management.md)处理。新建保持禁用，编辑保留原启停状态，启停须有独立的明确请求。
5. **核对保存和覆盖。** 核对实际返回的请求 ID、目标、内容／revision 和持久化回执，再用新鲜的 `content_read` 读回。客户端排队或暂存只表示本机已保留；有效的 `local-confirmed`、`local_durable:true` 确认 Server 保存，`remote-confirmed`、`durable:true` 确认 GitHub 备份。未知结果沿用同一请求 ID 与 CAS 参数；冲突按[队列与冲突恢复](https://github.com/goodname427/agent-brain-client/blob/main/docs/reference/memory-behavior.md#保存队列与冲突恢复)处理。按来源报告已核对、重复、排除、冲突、待确认、未读及不可访问的范围。缺少授权、工具、完整盘点或读回证据时报告具体缺口。即使完成本轮全部可访问来源，也只报告本轮实际授权范围的覆盖；安装回执、映射、目录数量和旧轮次都不能证明账号全量覆盖或模型已采用指令。交接报告只保留必要的状态和计数。

日常保存与召回见[记忆使用说明](https://github.com/goodname427/agent-brain-client/blob/main/docs/reference/memory-behavior.md)，状态、更新及同步恢复见[日常维护](https://github.com/goodname427/agent-brain-client/blob/main/docs/installation/maintenance.md)。
