# 单链接接入流程

Agent 先确认实际设备、Python 和 Harness，不按模型名冒用工具名称。当前只允许验证过的 Codex；其他 Harness 给出适配缺口，不执行试探安装。

运行 `python3 connect.py --agent codex` 做预检。它查客户端登记、已知 `.config/agent-brain-client/service-profile.json` 和已有 Brain 绑定，判断首次授权、复用、迁移或修复；不读取原生历史，不复制凭据，不执行数据 Git。Mac 默认选择受限 SSH MCP，token 留服务器；没有已批准 profile 时说明服务授权这个具体节点，不要求把 token/私钥发到聊天，也不默默生成新身份。

保留早期已授权 HTTP 客户端的 endpoint/token-file 参数与 `.config/agent-brain-client/device.json` 复用；这一兼容入口不把凭据交接加入当前 Mac SSH 方案。直接 HTTP 传输输入输出 JSON 行，只在服务只读检查成功后登记元数据，原有客户端和未知登记不覆盖。

Mac SSH 入口使用 `python3 connect.py --agent codex --service-profile <已批准的本机0600 JSON>` 检查。profile 精确声明 adapter/platform/data_mode，以及现有 SSH host/user/identity_file/known_hosts_file/known_hosts_sha256/remote_command；不含 token 或私钥值。Agent 根据实际 Harness 判断：首次授权缺口、可登记、可复用、漂移修复、旧入口迁移或其他适配器需开发，不让用户先分类设备。仅当配置与只读协议预检通过，才在已有授权内加 `--apply --plan-sha256 <刚检查的摘要>`。只增一个 MCP 表及一个合成托管块，保留其他设置，不生成身份、不改 hooks/Skill 或后台任务。

已有 codex-mcp.json 时先核对其自有配置或完整已批准迁移回执，再复用；有可信新登记和保留的旧项目登记不等于需要重新迁移。未知登记、重叠 legacy、改动的服务/身份/固定命令、托管标记或 shadowing override 给出具体 review 步骤，不覆盖现场。

初始化后 Agent 报告配置及协议检查结果，无需新模型或 GUI 验收。`client_startup.py --policy-file <固定受审policy> --policy-sha256 <固定摘要> --check-only` 可独立检查客户端更新；正式源未批准时保留 not-configured。固定 MCP 启动入口调用同一 ReleaseEngine，不另装调度器。新权限、协议、迁移或本地漂移先保留并报告具体操作。

已有本地 Brain 绑定而没有可信新登记时，保持 migration-review-required。首个受审 Mac 方案注册固定 SSH stdio 命令，服务器服务账户读取本机 token；获批固定 bootstrap 可改为 client_startup.py，传递固定策略摘要。Agent 独立准备入口、安全基线和备份，获批准后负责配置及自检。未批准身份、服务器代码或发布源时，给出具体缺口，不能将 HTTP API 直接当 Streamable HTTP MCP 地址。真实记忆与项目迁移不由这个合成入口授权。
