# Client 记忆接口与状态

日常保存/检索决策由当前加载的 `agent-brain-memory` Skill 定义；来源导入与覆盖决策由 `agent-brain-onboard` Skill 定义。本文件和 Client 的 MCP instructions 只说明工具、范围、回执及安装交接，不复制或覆盖这些 Skill 的行为规则。若相应 Skill 未加载，报告该能力缺口，不从 Client 文档推导另一套记忆政策。

`tools/memory_review.py` 只离线审阅调用者明确提供的非正文候选元数据，不发现来源、不授予权限、不采集、不上传。Onboard 模式的 `coverage-incomplete` 表示所提供元数据存在来源覆盖缺口；`provided-sources-reviewed` 只表示所提供的来源清单已审阅。两种回执都带 `account_complete:false`、`source_discovery_performed:false` 和 `data_transferred:false`，并报告增量候选数与等价记录数；它们不证明账户全量或迁移完成。输入无效时返回 `source-review-invalid`，且 `data_transferred:false`。

## 安装后的 Onboard 交接

安装与再次安装成功后，Client 返回 `coverage-check-required`、`source_scan_performed:false` 和 `data_transferred:false`。执行 Agent 按当前加载的 `agent-brain-onboard` Skill 继续；这些字段仅表示 Client 未执行来源扫描或迁移。

## 辅助query

memory_query(kind=memory,query='',since=null,until=null,limit=3)只读当前有效Memory，复用Core中英文词汇算法与服务器更新时间；时间必须带offset，最多3条preview/title/description/revision/updated_at，time_basis明确memory_updated_at，不冒称事件发生时间。先按actor/ID/ContentPolicy过滤再读/评分，旧record四字段不变，不默认查询历史旧偏好。其他类型blocked不阻止这个独立只读路径；离线不伪报新鲜查询。

当前已安装的MCP whitelist仍是catalog/read/put。因此content_catalog的Memory-only查询别名（kinds=[memory]，附query/since/until/limit）转到同一memory_query，不改注册、凭据、scope或安装信任；新工具名也有统一MCP声明，允许该名字的调用方可直接用。普通catalog形状/调用不变；旧后端明确不可用而非假空结果。Mac/Windows共享Core/MCP结构，不把纯协议测试宣称为Windows系统凭据/裸机支持。

## 本批边界

源码测试不写生产 Rule/Skill/Memory，不采集新来源或执行真实生命周期验收。未来新增短记或纠正复用 content_put；既有 read/put 不增加 delete/admin/activation。行为指引与查询的源码实现、发布、部署和实际运行验收分别报告。

安装结果交接和本机 status 随仓库安装入口源码交付；运行期 MCP 的新指引及 `model_loaded:"unknown"` 回执需发布并激活经过审核的活动 Client 包后才生效，运行中的旧 worker 不会热替换。共享三套 Skill 的正文如需调整，应另用受控内容更新流程，不能直接修改受管映射或仅凭源码更新宣称服务端 Skill 已改变。
