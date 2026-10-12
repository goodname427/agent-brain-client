# 记忆保存与首次迁移行为规范

重点是Agent知道何时保存，以及Onboard完整覆盖已授权已有记忆；不是再建同步平台。以下规范由content MCP启动instructions与owned原生映射共用，离线审阅工具memory_review只处理明确输入的来源/候选元数据，不采集、不授予权限、不上传。

## 日常新内容

在既有授权范围内主动判断普通偏好、纠正、个人事实、个人任务进展、短期连续上下文和有用琐事。重大决策、长期永久价值不是门槛。用户明确要求记住的小事实按要求保留；未确定推测不能写成确定事实。简短写日期/事实/结果/下一步，优先同稳定主题合并，读revision再完整CAS更新。不能把一次临时格式要求升为全局偏好，也不机械上传无后续用途的噪声。

用户在已授权对话提供的非敏感个人工作进展摘要，和采集项目文件/项目专属规则是不同操作。前者可用于连续上下文；后者如不在授权范围仍deferred，不用个人ID伪装项目隔离。公司敏感、密码密钥等秘密、LocalOnly、受禁sessions/数据库/raw历史排除，不用筛选规范创造新来源授权。

## Onboard已有记忆

默认完整增量迁移每个已授权且可读取来源中的所有已有记忆，除公司敏感及秘密/既有范围边界；不按重要性、时效长短或是否重大再挑小部分。已有不确定记忆保留来源和不确定性，而不是自动变成事实或因为不够确定丢弃。逐页/逐批枚举所有已授权候选；最多3个相关主题是日常检索预算，不是导入总量/来源覆盖配额。

等价记录跳过写入、记为duplicate（已覆盖）；不同记录先读取现有全文，保留双方独有信息，协调后才用原CAS提交。首次/再次Onboard不得仅因源版本变化自动替换一个已有Memory；主线实现把这种差异保留为conflict，需显式合并。Rule/Skill已有资源、trigger和enabled保护不变，不隐式启用。

报告每个来源可访问/已读/不可访问/未检查/排除及原因、已枚举数/已审阅数；每个候选candidate、duplicate、skipped、excluded、deferred、review及理由；上传另报imported/pending/conflict/blocked。覆盖缺口与编辑决策、重复、权限/运输/合约错误分开。不能用pending=0、一份显式文件清单或几个主题推断账号全量。新具体来源或敏感运输仍要核实授权。

## 相同材料的隔离行为用例

| 材料 | 日常 | Onboard（该内容已经是记忆且来源已授权） | 原因 |
| --- | --- | --- | --- |
| 用户普通咖啡偏好 | candidate | candidate | 有用个人偏好，不必重大 |
| 完成一个小步骤、下一步明确 | candidate | candidate | 短期连续性可用 |
| 有用琐事 | candidate | candidate | 可以帮助未来任务 |
| 本次临时字体要求 | skipped，除非用户明确记住 | candidate | 新内容不必噪声化；已有记忆不再按重要性过滤 |
| 未证实推测 | review，或按明确请求保留不确定性 | candidate并保留原不确定性 | 不编造事实、不丢已有来源 |
| 与目标等价 | duplicate | duplicate | 已覆盖，不重复上传 |
| 公司敏感/秘密 | excluded | excluded | 不是上传候选 |
| 原始聊天/受禁session | excluded | excluded | 不是“已有记忆”授权的延伸 |
| 未授权项目文件/LocalOnly | deferred/excluded | deferred/excluded | 真实范围边界 |
| 账号出口未检查/不可访问 | coverage gap | coverage gap | 不能宣称完整 |

测试采用有依据的合成候选元数据，并验证MCP/映射规范接入、完整Onboard默认、完整来源计数、显式请求、重复、秘密、越界与保留独有信息。它是政策用例，不声称模型真实理解或已遍历用户账户；不新增付费模型验收。

## 辅助query

memory_query(kind=memory,query='',since=null,until=null,limit=3)只读当前有效Memory，复用Core中英文词汇算法与服务器更新时间；时间必须带offset，最多3条preview/title/description/revision/updated_at，time_basis明确memory_updated_at，不冒称事件发生时间。先按actor/ID/ContentPolicy过滤再读/评分，旧record四字段不变，不默认查询历史旧偏好。其他类型blocked不阻止这个独立只读路径；离线不伪报新鲜查询。

当前已安装的MCP whitelist仍是catalog/read/put。因此content_catalog的Memory-only查询别名（kinds=[memory]，附query/since/until/limit）转到同一memory_query，不改注册、凭据、scope或安装信任；新工具名也有统一MCP声明，允许该名字的调用方可直接用。普通catalog形状/调用不变；旧后端明确不可用而非假空结果。Mac/Windows共享Core/MCP结构，不把纯协议测试宣称为Windows系统凭据/裸机支持。

## 本批边界

源码测试不写生产 Rule/Skill/Memory，不采集新来源或执行真实生命周期验收。未来新增短记或纠正复用 content_put；既有 read/put 不增加 delete/admin/activation。行为指引与查询的源码实现、发布、部署和实际运行验收分别报告。
