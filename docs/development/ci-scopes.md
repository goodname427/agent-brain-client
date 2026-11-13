# Client 检查分层

核对日期：2026-11-13。

PR、main push 和 merge queue 只运行快速阻塞检查。它们始终启动工作流并保留稳定的 `Client checks required` 汇总；失败、取消、缺失输出或跳过必需任务都不能成功。checkout 固定事件 SHA，检查 PR 合并后的树；范围从本地 Git 完整历史取得，不依赖 API 文件列表分页。

| 阶段 | 检查 | 完整发布证据 |
| --- | --- | --- |
| PR / main push / merge queue | 文档链接及 CI 合同回归、所有 tracked Python 语法、已提交 diff 空白、架构边界及范围映射中的短回归 | 否 |
| 发布前手动 Client checks | 原有完整测试、lint/type、平台原生及跨仓检查 | 全部任务真实成功后才是 |
| 发布准备及发布 | 现有完整包验证、精确源码树／attempt、不可变资产和回读门禁 | 仍全部必需 |

快速入口为 [quick.py](../../dev/checks/quick.py)。定向清单 `TARGETS` 随代码维护：CI 工具改动运行对应回归；发布工具改动运行发布门禁回归；Client 主机入口／采集控制和 Server 身份／回执合同改动另运行对应短回归。未映射的功能改动仍执行语法及架构检查，完整行为验证留在发布阶段；快速绿色不能声称该功能已经完整验收。任一定向测试失败或超时都失败，绝不转成跳过或成功。无法解析 diff 也失败，不能冒充完成检查。

普通事件的 `full=false`；Client 原生平台任务明确 skipped，Server 使用 Linux 执行快速检查。工作流手动 `workflow_dispatch` 无条件强制原有完整范围，不能用 PR 绿色代替。发布前在 **Client checks → Run workflow** 选择最终 main，等本次完整任务成功，再使用原有发布入口。不要在每个 PR 或普通 main push 重复运行全量。

发布证据门禁仍要求同次 attempt 中原有全部任务真实成功，包含 `Client full validation`。快速结果不能产生该标记。Server candidate 仅在手动完整检查成功后继续原有构建和包验证；普通 main 的快速运行不会构建候选。Client release 的 prepare/publish 权限隔离及所有平台证据要求保持原合同。

本地完整入口 [client.py](../../dev/checks/client.py) 保留。Actions 不可用时，Client 使用既有 local/hybrid 回执与原生 Windows 双版本证据，Server 使用既有 prepare-local；它们仍运行完整检查，不伪造 Actions 成功。详细命令见原发布文档。范围分类器的 `plan` / `tests` 命令保留供本地诊断，它们描述可选的旧范围计划，不是日常 Actions 的分层策略。

实际耗时必须区分排队、runner 设置及执行时间；减少工作量不是实测加速数据。CI 自身变更用定向合同／发布拒绝用例验证，之后首次发布仍必须取得最终树的完整证据。此改动不创建版本、tag 或改动任何设备功能。
