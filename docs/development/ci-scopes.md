# Client 检查分层

PR、main push 和 merge queue 运行既有快速检查。`Client checks required` 检查各项必需任务的结果。工作流固定 checkout 的事件 SHA，在 PR 合并后的树上执行检查。变更范围取自本地完整 Git 历史。

| 阶段 | 检查 | 完整发布证据 |
| --- | --- | --- |
| PR / main push / merge queue | 文档链接及 CI 合同回归，已跟踪 Python 语法、提交范围空白、架构边界、公开扫描及定向回归 | 否 |
| 发布前手动 Client checks | 完整文档、源码、macOS 和 Windows 检查，含 lint、类型检查及原生回归 | 全部必需任务成功后才是 |
| 发布准备及发布 | 现有完整包验证、精确源码树／attempt、不可变资产和回读门禁 | 仍全部必需 |

文档任务负责文档链接及 CI 合同回归。[快速源码检查](../../dev/checks/quick.py) 检查事件提交范围的空白，运行架构回归、公开扫描和源码边界检查。`TARGETS` 为主机入口、采集控制、发布工具及其他匹配路径追加对应回归。无法解析提交范围、检查失败或超时都会使快速检查失败。

普通事件的 `full=false`，源码任务照常运行，macOS 和 Windows 原生任务跳过。发布前在 **Client checks → Run workflow** 选择最终 main。手动触发运行完整检查，可同时启动发布资产准备。

发布在写入远端前核验同树、同次 attempt 的完整成功结果，包含 `Client full validation`。资产准备复用该结果，并执行四架构包构建、自检、公开扫描及资产校验。发布权限和回退见[发布流程](release-pipeline.md)。

[本地完整检查入口](../../dev/checks/client.py) 提供各阶段的完整命令。Actions 不可用时，按发布流程生成 local/hybrid 回执和原生 Windows 3.11、3.13 证据。范围分类器的 `plan` 和 `tests` 仅供本地查看定向范围。
