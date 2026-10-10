# 单链接接入流程

Agent 先确认实际设备、Python 和 Harness，不按模型名冒用工具名称。当前只允许验证过的 Codex；其他 Harness 给出适配缺口，不执行试探安装。

用户入口是默认公开仓库链接，main 必须提供当前初始化说明和与生产发行一致的客户端实现。正式发布同时核对 main 与批准的生产 release ref 的 payload/版本清单摘要；两条公开历史可分别正常提交，不复制私有历史，不强推。实际更新只按固定 origin/ref/anchor/channel/capabilities，不凭版本号或分支名选源。现有个人生产路径为 releases/personal-stable；releases/stable 的旧 synthetic-only 发行不作为个人设备回退源。切换为 19 文件内容范围须有新政策批准；源或能力不匹配时保留已验证个人包并报告阻碍。

旧设备或本地 Brain 已安装时，同时读取 [旧版迁移与可恢复清理](ADAPTERS.md#旧版迁移与可恢复清理)，先保存依赖及保留清单。

运行 `python3 connect.py --agent codex` 做预检。它查客户端登记、已知 `.config/agent-brain-client/service-profile.json` 和已有 Brain 绑定，判断首次授权、复用、迁移或修复；不读取原生历史，不复制凭据，不执行数据 Git。Mac 默认选择受限 SSH MCP，token 留服务器；没有已批准 profile 时说明服务授权这个具体节点，不要求把 token/私钥发到聊天，也不默默生成新身份。

保留早期已授权 HTTP 客户端的 endpoint/token-file 参数与 `.config/agent-brain-client/device.json` 复用；这一兼容入口不把凭据交接加入当前 Mac SSH 方案。直接 HTTP 传输输入输出 JSON 行，只在服务只读检查成功后登记元数据，原有客户端和未知登记不覆盖。

Mac SSH 入口使用 `python3 connect.py --agent codex --service-profile <已批准的本机0600 JSON>` 检查。profile 精确声明 adapter/platform/data_mode，以及现有 SSH host/user/identity_file/known_hosts_file/known_hosts_sha256/remote_command；不含 token 或私钥值。Agent 根据实际 Harness 判断：首次授权缺口、可登记、可复用、漂移修复、旧入口迁移或其他适配器需开发，不让用户先分类设备。仅当配置与只读协议预检通过，才在已有授权内加 `--apply --plan-sha256 <刚检查的摘要>`。只增一个 MCP 表及一个合成托管块，保留其他设置，不生成身份、不改 hooks/Skill 或后台任务。

已有 codex-mcp.json 时先核对其自有配置或完整已批准迁移回执，再复用；有可信新登记和保留的旧项目登记不等于需要重新迁移。未知登记、重叠 legacy、改动的服务/身份/固定命令、托管标记或 shadowing override 给出具体 review 步骤，不覆盖现场。

初始化后 Agent 报告配置及协议检查结果，无需新模型或 GUI 验收。`client_startup.py --policy-file <固定受审policy> --policy-sha256 <固定摘要> --check-only` 可独立检查客户端更新；正式源未批准时保留 not-configured。固定 MCP 启动入口调用同一 ReleaseEngine，不另装调度器。新权限、协议、迁移或本地漂移先保留并报告具体操作。

已有本地 Brain 绑定而没有可信新登记时，保持 migration-review-required。首个受审 Mac 方案注册固定 SSH stdio 命令，服务器服务账户读取本机 token；获批固定 bootstrap 可改为 client_startup.py，传递固定策略摘要。Agent 独立准备入口、安全基线和备份，获批准后负责配置及自检。未批准身份、服务器代码或发布源时，给出具体缺口，不能将 HTTP API 直接当 Streamable HTTP MCP 地址。真实记忆与项目迁移不由这个合成入口授权。

迁移成功后按上述清理指引核对新入口独立依赖与唯一数据，再将确定被替代的对象移到系统废纸篓或已核验的可恢复位置。未迁移 Rules/Skills、私有数据、凭据、当前开发工作区及新客户端引用的文件均保留；旧 hooks 已停用时不扩大安全设置修改范围。


## Mac Codex 内容客户端候选闭环

现有真实 SSH 五工具入口继续按原登记运行。新的内容路径是单独候选，不因读取本链接而取得配对、签名、网络、真实内容迁移或扩展发布授权。先识别实际 Harness/平台、已有登记、经验证版本、旧写入口和私密计划；非 Mac Codex 报 adapter-review-required。未知或未获批的签名/访问组/服务目标先报告具体缺口，不寻找 token、私钥、原生会话或助手自定义审批内容。

获批并准备签名组件及私密 profile/plan 后，组件 `--pair` 使用安全输入短期码；有专门批准的 issuer policy 才用 `--operator-pair`，管理员授权经原生安全输入，短期码在组件内直接配对。Pairing metadata 的 installed=false 是正常的：还需原生安装。使用已批准私密 release policy 和 19 文件内容包，由 content_install.py plan 固定实际 home、输入/包/原生前后哈希；替换当前 MCP 必须有可验证的自有登记和明确替换计划。

获授权后以精确计划摘要 apply：固定 read-only 安装检查、一个 MCP 表/登记写入（有已证明所有权的旧指令块时，明确增加 AGENTS.md 第三文件退役）、再次协议检查与精确失败恢复。成功即可说明“安装成功，配置和协议已验证”，model_loaded=false；不把额外模型行为验收设为用户必做或要求新开会话。普通 connect 识别新登记和活动版本；启动按明确受信政策检测更新并复用 ReleaseEngine 健康／失败恢复，缺源／不兼容／本地漂移有明确状态。真实内容读取/维护仍以 frozen grant 与 Server 内容政策为准，不自动迁移 Rules/Skills、采集真实对话或新增后台。


## 最小 SSH 内容候选（尚未启用）

优先评估复用既有受限 SSH 身份的 `ssh-fixed-proxy`：Mac 不接收 API/管理员凭据，不需要 Apple 账户、Keychain 新授权或公网 API。固定服务器代理在服务账户下用私密设备记录访问原有 `127.0.0.1:8765`；当前已安装的五工具强制命令不能处理新 JSON 代理。必须批准精确受限公钥条目的固定命令切换、Server 配对/内容政策及记录、Mac 安装/映射和发布范围；这不是现有身份自带的新权限。保留 restrict、无 shell/PTY/转发和严格 host key，单条连接仅存在于 MCP 进程生命周期，不安装隧道、ControlPersist 或后台。已批准身份可用时无需用户再次输入秘密；一次确认范围即可，后续可验证准备由 Agent 完成。

`personal-content-ssh` 是单独的 19 文件源代码候选：将签名候选的 Swift payload 换成 ssh_content.py，仍保留 client_pairing.py 的中立范围校验。capabilities=[codex-stdio-mcp,personal-content-explicit,ssh-server-credential-proxy]；旧 13 文件及 signed-device-proxy 政策都拒绝混用。复用同一 release engine、content MCP、原生安装和恢复检查，不暗改签名候选的校验。

Server 凭据生成与内部交付由另获批准的精确私密计划执行：先持久保存 pending 记录，再 confirm；只输出 device ID、政策摘要等元数据。Mac 仅收到无凭据的 profile，模型不收到密钥、配对码或 Bearer。参数计划先生成不含秘密的政策／配置；确认后的 Server receipt 给最终 proxy policy SHA，才能准备真实 native plan。现阶段只有代码及合成检查，不能说已部署。

新设备缺 SSH identity、服务器明确授权或可信 host key 时，本链接只能检查并给出具体缺口；不能自动生成身份/改公钥登记，也不能宣称一次输入或零配置。用户通过系统安全交互选择既有授权身份并核对 host key，Agent 再在冻结的固定命令和发布范围内准备初始化。现有 Mac 复用已验证身份无需再输入秘密；单次范围批准不等于新设备身份已建立。

服务内容默认经 catalog/read/put 三个授权工具加载和维护，read/put 范围不含 remove；旧 write 不隐含删除。云端 Skill 必须明确适配到该接口，不能继续依赖旧本地 Core/Git。转换先在私有暂存中保留源资源、启用/触发及摘要，默认未知 Skill disabled；具体迁移、启用和 namespace 获批才上传/映射。当前日常范围不支持项目、LocalOnly、任何类型删除、split 或 section；Agent 给出边界，不把 unsupported 操作改成个人全局数据。新适配器开发按 ADAPTERS.md 契约在已授权仓库完成并验证，不以静态文件存在冒称已支持。
