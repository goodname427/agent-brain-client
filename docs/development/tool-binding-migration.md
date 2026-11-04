# 跨版本工具清单与固定启动器迁移

> 当前维护；适用 Mac/Windows 共用 HTTPS 更新路径。核对日期：2026-11-04。合成验证不代表 Windows 宿主已重载。

## 两次问题的证据与边界

0.5.5 的问题是同一安装链路的三个生成入口不同步：计划按服务端 grant 取 catalog/read/put 并排除 memory_query，产生三项；安装检查另加 request-ID 与四个来源工具，产生八项；实际 Memory read 服务还有 memory_query。`16e1e88425d12cae19a699116b76e2fc6f0f1f3e` 将计划、安装检查与服务声明统一到 `content_mcp.enabled_tools`，并迁移确认为未执行的旧三项计划。完整经过与当时验证见[0.5.6 归档](../archive/0.5/release-0.5.6.md)。

这次是已完成安装的跨版本登记检查：旧登记保存九项，当前源码因 memory_recall 增为十项。`registered_context` 校验旧固定包时，经 `context` 调用了检查器自身的 enabled_tools，因此即使登记摘要完全匹配，也会以新的十项清单拒绝旧的九项。原兼容分支只认可减去本地工具和 memory_query 后的三项，不涵盖旧完整九项。原测试用同一源码构建各版本号，覆盖了声明扩展与未执行计划恢复，但未模拟“冻结旧声明 + 新检查器 + 活动包已前进”的组合。

两次都涉及工具合同来源不一致，但前次发生于首次安装/未执行计划，当前缺口位于已完成登记的版本边界。不是把旧三项恢复再执行一次即可解决，也不能靠手改白名单或摘要修复。

## 修复与权限边界

`context` 先完成不可变包的成员/摘要和原策略验证，再从该目标包自己的工具合同计算清单；不拿当前检查器的清单代替目标包。相同声明的常规路径复用本地实现，不同历史声明通过有限时、有限输出的隔离 Python 子进程读取。子进程只导入已验证 Client 模块并计算 policy/grant 对应的工具名称，不创建客户端、不读取凭据、不执行初始化、映射或网络请求。错误时保留登记并明确报告，不退回猜测当前工具列表。

因此，旧登记由旧包的九项合同核验；正常 update 的目标计划由新包的十项合同生成。原有登记摘要、解释器、参数、环境、策略/身份、事务前后像和外部编辑检查继续生效。迁移沿既有固定启动器事务执行，暂停状态、无关宿主设置、旧包及队列保留。只读 check-update 不写配置，update 不配对、不强杀 worker，也不增加原 read/put/artifact/ID 或采集权限。

后续新增工具无需再登记“九项/十项/十一项”例外名单。测试同时覆盖真实冻结九项合同、新包十项，以及合成未来十一项；即使源码检查器领先已发布包，也必须使用目标包自身的合同。协议 schema 的不兼容变化仍须单独迁移，不能由这一机制豁免。

## 验证与恢复

`test_package_tool_binding` 覆盖旧登记/新活动包、只读检查、固定启动器替换、身份和无关配置保留，以及工具/命令/环境漂移拒绝。`test_platform_checkpoint.HistoricalPackageTools` 在既有 Mac/Windows 测试入口验证冻结目标合同、未来新增工具和探测失败，不改工作流权限。

活动包版本、登记启动器版本及宿主 worker 版本分别报告。修复发布后仍须走官方 update，再正常重载 Agent Brain，并从正式 MCP 的 client_runtime 确认新 worker；活动包已更新不能证明已有宿主进程已重载。此前附件未保留完整诊断输出，本次结论以冻结源码和合成复现为据，生产设备仍需复核。

## 重启后工具仍不可见的后续检查

后续 Windows 验收附件证明正式 worker 已运行 0.6.6，但模型仍报告没有 `memory_recall`；附件未提供原生 `enabled_tools` 或宿主收到的 `tools/list`，因此不能据此认定 Windows 安装器或宿主缓存是直接根因。

独立排查确认另一个可复现缺口：旧三工具登记在只读检查时应保持不变，但 `prepare(replace_existing=True)` 也复制了旧过滤清单；若目标包已经与登记启动器相同，更新入口还会直接跳过替换。结果是正常更新和重启都无法修复这类登记。修复后，显式更新按已验证目标包及原 policy/grant 生成替换清单，同包但清单不同也执行原有事务。只读检查仍不改登记，暂停状态、身份、信任锚、外部编辑保护和待发送队列保留。

`check-update` / `update` 的 `tool_discovery` 分别报告 `active_package_tools`、`registered_enabled_tools` 和 `blocked_by_registration`。同版本缺少工具时仍报告需要更新。MCP 回执的 `client_runtime.declared_tools` 报告实际 worker 声明的清单；`host_tool_discovery` 与 `model_tool_visibility` 始终为 `unknown`，不能把包声明或配置匹配冒称模型已可调用。普通 `memory_query` 的说明明确区分 topic 检索与分层召回。

回归覆盖旧三工具同包修复、九到十项迁移、暂停与外部编辑保护，以及待发送文件逐字节保留。源代码和实际构建包的离线 MCP checkpoint 核对计划清单、`tools/list` 与 worker 声明，在现有 Mac/Windows CI 入口执行。最终仍需在两端宿主中确认模型发现 `memory_recall` 并完成受控 L2/L3 调用；这里的合成验证不作该项声明。
