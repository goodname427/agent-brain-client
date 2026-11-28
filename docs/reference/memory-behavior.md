# Client 记忆使用、接口与状态

> 当前维护。适用 Client 0.8.31 源码，核对日期 2026-11-22。实际可用工具以宿主 MCP 清单和已授予范围为准。

## 日常记忆

使用已接入的 `agent_brain_server`。明确授权当前宿主的可共享个人原生记忆和既有接收目标后，请宿主运行 Agent Brain Onboarding（`agent_brain_server.memory_onboard`）。工具返回批次 `approval_id` 后继续后台导入；用相同宿主、来源范围及接收目标，传入此 ID 和 `resume=true` 查询同批进度，无需再次批准或提交。接收计数来自 L0 回执，不代表提炼已完成。授权内的长原文不转写为普通对话：支持长原文的 Server 按已有 128 KiB 原生文件边界，原样接收一条 L0；超过边界或传输失败会单独报告，不截断原文。日常保存、检索和维护可直接使用现有工具。

同一来源快照仍在处理或等待恢复时，重复启动会返回原批次。批次结束后，经用户明确批准再次启动，会生成新批次，重新判断当前仍获授权且缺少 L0 回执的原文。已收到的原文计为既有记录，旧批次仍可用原 ID 查询。

Onboarding 的目标是一个已接入并获授权的个人路由。启动前确认该次批准的全部原文均可发送到此目标，包括其中获准共享的项目来源。项目归属未知时，Client 保留原生来源 ID。当前公开原生事件协议由 Server 按获准的目标 origin 绑定 L0 命名空间，逐条项目范围需另行支持。

- **查找和使用。** 用 `memory_query` 按当前任务和来源范围查找，必要时用 `content_read` 读取选中主题的正文与 revision。核对来源和适用范围；其他项目的经验只作为相关参考。离线缓存或找不到合适结果时如实说明。列出完整目标清单才用 `content_catalog` 并遍历全部页面。
- **选择要保存的内容。** 在现有授权内保存用户明确要求记住的合适内容、用户陈述的事实／偏好／纠正、已核实的成果和可复用的经验。普通事实、短期进度有后续用途就可保留；不要把推测、一次性闲聊或整段会话写成记忆。按原范围处理项目专属内容；只把能安全泛化的部分写入个人共享池。秘密、LocalOnly、客户或雇主机密留在原处。
- **新建或更新。** 先找相关主题，用新鲜的 `content_read` 确认记录和当前 revision。相同事实无需重写；纠正对应旧事实，更新保留仍适用的内容与来源。用 `content_put` 提交 `title`、`description`、`body`、`type`（`user`／`feedback`／`reference`）、读回的 `expected_revision`。首次新建使用读回的 `missing`。Client 为每次新写入分配请求 ID，并在发送前保存原参数和版本基线。用实际回执核对 Server 保存和备份状态，必要时读回当前版本。
- **自动记忆。** 已登记并授权的 Hook 处理今后的实际对话，遵循现有范围、暂停与来源约束。分别核对实际 Hook 投递、Server 事件接收、派生记忆及召回；其中一个成功不证明其他步骤已完成。按[项目记忆](automatic-memory.md)或[已配置的全局记忆](global-memory.md)查看原入口。

本仓库的 `src/agent_brain_client/interfaces/cli/sources.py` 可审阅调用者明确提供的非正文候选元数据。它仅处理所给数据；实际来源盘点、授权和保存证据来自正式工具及回执。

## 保存队列与冲突恢复

离线或回执丢失时，已保留写入返回 `content_upload_queued`、`local_queue.state=pending` 和内部 `request_id`。恢复连接后 Client 自动沿用原参数、CAS 基线和 ID 补发。服务器保存以 `persistence` 回执为准，本机映射和模型加载分别核对。

有效的内容写入回执另附 `write_status`，首先明确“服务器已保存”。GitHub 备份单独标为 `confirmed`、`pending` 或 `needs-attention`。Server 返回正常 `automatic_retry.state=waiting` 时无需重复写入或轮询；暂停、需审查和 `attention_required=true` 会明确提示处理备份，MCP 保存结果仍为成功。原始 `persistence`、备份诊断及本机队列完整保留；旧 Server 缺少重试元数据时只报告备份尚未确认，不推断后台正在工作。离线、CAS 冲突及非法回执不会得到保存成功摘要。此摘要不用于自动采集的 Core 接收回执。

已明确拒绝的 `conflict` 和 `blocked` 保留原始提案、错误及冲突快照，但不再阻止新请求。新保存直接使用 `content_read` 的当前 CAS 版本；服务器仍拒绝过期版本，不自动覆盖或重定基线。未知结果的 pending 仍先按原请求和原 ID 恢复，不能通过换 ID 绕过。

在官方 Client checkout 中运行以下只读本机诊断。Codex 和 CodeBuddy 使用各自的本机缓存，`--harness` 填实际写入的宿主。

```sh
sh bin/memory.sh content-manage --harness codex --inspect-queue
sh bin/memory.sh content-manage --harness codex --inspect-queue --request-id <request_id>
```

Windows 使用 `& ./bin/memory.ps1 content-manage --harness codebuddy --inspect-queue`，选项相同。列表每页最多 64 条，包含待确认、冲突和阻断请求的元数据。续页传 `--cursor <next_cursor>`，队列变化后从第一页重查。按 ID 检查会显示原始请求（含正文）、CAS、保留回执与已缓存的冲突版本。在本机私下查看这些详情。该命令要求既有写入授权，仅读已绑定缓存，无需访问 Server。

对于 CAS 冲突，保留原始提案，使用 `content_read` 读取当前版本，并审阅本地提案和远端版本。显式确认合并后，用当前 `expected_revision` 和 `reconciles.request_ids` 指向确切冲突，提交新的 `content_put`。已有 pending 请求的参数及 reconciliation 引用不可修改。只有合并请求取得有效服务器保存回执后，旧冲突才标为 `reconciled` 并保留 `resolution_request_id`；丢失回执时沿用合并请求原 ID 恢复。后续原请求仍使用自己的 CAS 基线，过期时会明确冲突，不自动覆盖或重定基线。未选中的冲突继续保留，供后续审阅，不阻塞其他有效 CAS 写入。

非 CAS 的 `blocked` 请求需先审阅原错误、修复原因，再显式重试完全相同的请求；刷新不会自动重试它。禁止通过删除队列或修改用户数据解除阻塞。实际设备队列的诊断和恢复验收需另行进行，合成测试不能证明 Windows 或 Mac 上已有 pending 已送达。

合并确认后，同次队列处理会回头继续处理排在合并请求之前的等待项。发送遇到丢回执或其他未确认结果时立即停止，该调用随后的映射刷新不会再次发送。结果不明的 pending 由既有后台刷新或 MCP 重连自动恢复；确认前用本机诊断查看状态。全过程保留原请求、原 reconciliation 引用和 CAS。

## 查询接口

正式 MCP 的 `memory_query` 只需 `query`。当前目录来源在安装包内读取。普通目录可按 `non-project` 查询，项目来源省略 `repository_root` 时复用 `project_root`。L1 默认每 namespace 5 条，原生查询按相关性和字符预算装配上下文，L2／L3 另行组装。安装刷新保留两平台的召回设置。详见[共享 Hook 合同](../development/layered-recall.md)。

`content_catalog` 负责目标目录。先传 `kinds`，再按同一 `kinds` 传回 `structuredContent.result.next_cursor`；末页省略该字段。每页最多 256 条，工具没有 `limit` 参数。旧 `memory_recall` 和目录检索别名已移除。

## 内容及映射边界

新增短记或纠正复用 `content_put`。如用户明确要求遗忘，仅在宿主公开已授权的 `content_lifecycle(action=remove)` 时执行移除，避免自动恢复已移除的条目。Rule／Skill 的明确用户启停使用 `content_put` 的 `operation=set_enabled`，普通正文编辑保留 `enabled`。参见[启停与同步](rule-skill-management.md)。

安装交接随安装入口源码交付。运行期 MCP 指引随受信活动包及实际 worker 生效。源码、映射文件或 `model_loaded:"unknown"` 均不能证明当前模型加载了内容。设备维护见[日常维护](../installation/maintenance.md)。

## 手动记忆的来源

Memory 写入自动附带当前目录元数据；宿主提供 `CODEX_THREAD_ID` 时记录该真实会话，也可显式传 `source_context`。不可用的会话或位置记为 null，不从正文猜测，也不沿用服务端迁移会话。来源在请求首次入队时固定，重启及重试复用原请求。旧客户端未提供来源时返回 `unknown-source`；历史记录中已知设备不一致的迁移来源只在读取时标为未知位置与会话，保留原账本。Server 应先于 Client 升级以支持可选字段。
