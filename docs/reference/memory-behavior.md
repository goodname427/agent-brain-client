# Client 记忆接口与状态

日常保存/检索决策由当前加载的 `agent-brain-memory` Skill 定义；来源导入与覆盖决策由 `agent-brain-onboard` Skill 定义。本文件和 Client 的 MCP instructions 只说明工具、范围、回执及安装交接，不复制或覆盖这些 Skill 的行为规则。若相应 Skill 未加载，报告该能力缺口，不从 Client 文档推导另一套记忆政策。

`tools/memory_review.py` 只审阅调用者明确提供的非正文候选元数据，不发现来源、不授予权限、不采集、不上传。

## 保存队列与冲突恢复

`local_queue.saved:true` 只证明请求已保留在本机；服务器保存仍以 `persistence` 回执为准。离线或回执未确认时返回 `content_upload_queued`、`retryable:true`，恢复连接后沿用原请求正文、CAS 基线和 `request_id`。本地映射刷新不等于服务器保存，也不会重载当前模型上下文。

同一目标前面存在未解决的 `conflict` 或 `blocked` 请求时，后续请求仍保留 `pending`，返回 `content_queue_blocked`、`retryable:false`；`local_queue.blocked_by` 列出实际阻塞的请求 ID 与状态，`next_action` 说明恢复步骤。这表示先解决依赖才能继续，不能把它报告成普通网络等待，也不能假定所有 pending 都来自冲突。这些诊断由现有队列计算，不改写队列格式、请求内容或 ID，不返回记忆正文或本地路径。

`content_queue_inspect({})` 只读本机保留队列，每页最多 64 条元数据，使用 `next_cursor` 继续；队列变化后重新从第一页检查。它列出请求 ID、目标、状态、CAS 基线和阻塞项，不发送或重试请求。`content_queue_inspect({"request_id":"…"})` 返回该请求原文、`reconciles`、原回执与已缓存的冲突版本，供 Agent 完整审阅。缓存版本可能过期，合并前仍通过 `content_read` 读当前版本。该工具仅在现有内容写入授权内声明，不扫描其他设备、原生记忆来源或历史会话；普通更新按同源工具契约登记它。

对于 CAS 冲突，保留原始提案，使用 `content_read` 读取当前版本，并审阅本地提案和远端版本。显式确认合并后，以新的稳定 `request_id`、当前 `expected_revision` 和 `reconciles.request_ids` 指向确切冲突，提交新的 `content_put`。这是新的合并操作，不能仅换 ID 重发原写入。已有 pending 请求的参数及 reconciliation 引用不可修改。只有合并请求取得有效服务器保存回执后，旧冲突才标为 `reconciled` 并保留 `resolution_request_id`；丢失回执时沿用合并请求原 ID 恢复。后续原请求仍使用自己的 CAS 基线，过期时会明确冲突，不自动覆盖或重定基线。未选中的冲突继续阻塞。

非 CAS 的 `blocked` 请求需先审阅原错误、修复原因，再显式重试完全相同的请求；刷新不会自动重试它。禁止通过删除队列或修改用户数据解除阻塞。实际设备队列的诊断和恢复验收需另行进行，合成测试不能证明 Windows 或 Mac 上已有 pending 已送达。

合并确认后，同次队列处理会回头继续处理排在合并请求之前的等待项。发送遇到丢回执或其他未确认结果时立即停止，该调用随后的映射刷新不会再次发送。结果不明的 pending 不能被合并标记为已解决；应取回原请求及原 reconciliation 引用，显式原样重试以恢复同 ID 回执。全过程保留原请求、用户内容和旧冲突，不清队列、不改 CAS。

## 安装后的 Onboard 交接\n\n安装与再次安装成功后，Client 返回 `coverage-check-required`、`source_scan_performed:false` 和 `data_transferred:false`。执行 Agent 按当前加载的 `agent-brain-onboard` Skill 继续；这些字段仅表示 Client 未执行来源扫描或迁移。\n\n## 辅助query

memory_query(kind=memory,query='',since=null,until=null,limit=3)只读当前有效Memory，复用Core中英文词汇算法与服务器更新时间；时间必须带offset，最多3条preview/title/description/revision/updated_at，time_basis明确memory_updated_at，不冒称事件发生时间。先按actor/ID/ContentPolicy过滤再读/评分，旧record四字段不变，不默认查询历史旧偏好。其他类型blocked不阻止这个独立只读路径；离线不伪报新鲜查询。

已有安装的旧MCP whitelist可能仍是catalog/read/put；0.5.6新计划按同源工具契约启用既有grant允许的工具，不静默改写旧注册。因此content_catalog的Memory-only查询别名（kinds=[memory]，附query/since/until/limit）转到同一memory_query，不改注册、凭据、scope或安装信任；新工具名也有统一MCP声明，允许该名字的调用方可直接用。普通catalog形状/调用不变；旧后端明确不可用而非假空结果。Mac/Windows共享Core/MCP结构，不把纯协议测试宣称为Windows系统凭据/裸机支持。

## Codex 原生持久记忆导入（Mac / Windows）

`content_sources_inventory` 只盘点当前绑定的 `CODEX_HOME/memories` 树，返回既有 `codex-memory:<相对路径 SHA-256>` 来源 ID、文件内容 SHA-256 和状态，不返回正文或本地路径。中文目录和文件名使用 UTF-8 计算 ID，子目录分隔符统一为 `/`；两平台使用同一格式。默认限制为 1024 个目录条目、1024 个文件、16 层子目录、单文件 128 KiB、总正文 16 MiB。缺少原生目录返回 `unavailable`，遍历受限或文件不可读返回 `incomplete`，不冒称已完成账户覆盖。

Windows 使用 `CreateFileW` 的 `OPEN_EXISTING`、`FILE_FLAG_OPEN_REPARSE_POINT` 和 `FILE_FLAG_BACKUP_SEMANTICS`，检查实际句柄类型和 reparse 属性；Windows 记忆入口保留配置路径，避免绑定路径规范化掩盖 `CODEX_HOME` junction；从路径锚点到原生目录的各级目录在整个操作期间保持打开，仅共享读取。junction、符号链接、挂载点和其他 reparse 来源不跟随；正文句柄同样排斥写入和删除。目录或文件正忙、枚举后对象被替换或无法验证时返回不可用，不降级为普通路径读取。句柄会在成功、失败或触及遍历限制后释放。此实现不修改原生文件、DACL、设备来源授权或安装信任。

调用者明确选择一条来源后，`content_sources_read` 重新进行有界遍历，核对打开对象与枚举对象身份，再核对读取前后元数据、大小和内容指纹，严格按 UTF-8 解码。正文只返回本地 MCP 调用者；盘点和审阅账本不保留原文。文件替换或内容变化后，旧指纹不能读取新正文；重新盘点得到新指纹后重新读取与审阅。

后续继续使用既有 `content_sources_review`、`content_sources_stage` 和 `content_put` CAS / outbox / 持久化回执流程。读取后才可审阅，只有 `shareable` 来源可以暂存；暂存本身不上传。显式 `content_put` 离线时保留同一请求 ID，重启重放并收到服务器持久化回执后才报告远端确认。

`memories_v2`、原生记忆 SQLite 和聊天 `sessions` 不属于此导入契约，不自动回退或扩大扫描范围。若某个 Codex 版本只写 `memories_v2`，需先另行确认其原生持久记忆契约；本实现仍只报告 `memories` 的实际可用状态。原生历史导入与未来 Hook 自动采集是两个独立状态；支持导入不会开启 Hook。

回归检查：隔离 `HOME` / `USERPROFILE` / `CODEX_HOME` 后运行 `python -B -m unittest test_codex_memory_inventory test_windows_native`（`PYTHONPATH` 包含 `scripts`、`adapters/codex`、`tools`、`tests`）。现有 Windows CI 已选择 `test_windows_native`；其中原生记忆测试继承完整共享导入套件，并增加 junction、竞争写入、目录固定及中文原生路径离线重启检查。Windows 原生 API 测试在其他系统跳过；Windows 缺少符号链接权限时仅跳过需要该权限的测试，junction 测试仍需执行。

## 本批边界

源码测试不写生产 Rule/Skill/Memory，不采集新来源或执行真实生命周期验收。未来新增短记或纠正复用 content_put；既有 read/put 不增加 delete/admin/activation。行为指引与查询的源码实现、发布、部署和实际运行验收分别报告。

安装结果交接和本机 status 随仓库安装入口源码交付；运行期 MCP 的新指引及 `model_loaded:"unknown"` 回执需发布并激活经过审核的活动 Client 包后才生效，运行中的旧 worker 不会热替换。共享三套 Skill 的正文如需调整，应另用受控内容更新流程，不能直接修改受管映射或仅凭源码更新宣称服务端 Skill 已改变。
