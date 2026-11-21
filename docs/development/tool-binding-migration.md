# 工具声明与固定启动器更新

> 当前维护；适用本次分层源码的执行布局。旧版本迁移经过保存在 [历史记录](../archive/0.6/tool-binding-migration.md)。

`application/tools/catalog.py` 是内容工具名称、schema、描述和 grant 过滤的唯一来源。安装计划、只读安装检查和 MCP 的 `tools/list` 使用这一合同；日常记忆检索统一为 `memory_query`。代码组织变化不增加原 read/put/artifact/ID 或采集范围。

安装器先核验目标包的完整成员和摘要，再从它自己的 catalog 计算声明。同一实现复用已加载模块；不同目标包通过有限时、有限输出的离线描述入口读取，不能用检查器版本的猜测清单代替。此机制只接受当前完整执行布局；没有旧九项、十项清单或历史路径的兼容例外。对应测试是 `test_content_install`、`test_platform_checkpoint`、`test_architecture`。

`adapters/installation/launcher_update.py` 负责 Mac/Windows 当前固定启动器的事务和中断恢复。只读检查不改登记。显式更新沿用原策略、信任锚、解释器、身份、权限和暂停状态，保留第三方配置和原请求队列。

受管 HTTPS 的 Codex 和 CodeBuddy MCP 使用同一个标准库单文件入口持有 stdio 和会话。业务进程按需启动。更新时，原业务进程处理完在途请求并退出，新进程核对已协商的协议、工具和缓存合同后接收后续请求。连接保留原工具合同，结果不明的写请求返回未知结果，客户端不自动重放。空闲后业务进程释放缓存租约，下一次请求可以启动兼容的已验证包。

如果新业务进程拒绝已协商的合同，会话可重新启动最近一次通过握手的 worker。即使后续更新覆盖了发布日志中的前驱，启动器仍在会话内记住这一个已授权入口。重启前重新核验登记、完整包内容和合同，不创建额外包副本或持久记录。

从 0.8.27 更新到单文件入口时，正常更新事务迁移原登记，宿主使用原生操作重新连接一次。随后普通业务文件增改沿用已有登记，由更新器完整校验包成员和摘要。原生 Hook 命令仍通过已登记包和原权限验证活动快照。正式同连接验收使用首版后的下一次普通业务更新。

Mac 的隔离回归通过 `test_mcp_single_entry_release` 使用精确 0.8.27 运行包和本地候选。检出须包含 `client-0.8.27-macos` 对象，按开发检查说明隔离 `HOME` 和 `CODEX_HOME` 后运行 `AB_REQUIRE_SINGLE_ENTRY=1 PYTHONPATH=src:.:tests python3.12 -B -m unittest -v test_mcp_single_entry_release test_mcp_worker_handoff`。

`tool_discovery` 报告活动包的工具声明、登记过滤和被过滤工具。MCP 的 `client_runtime.offered_tools` 表示实际 worker 提供的工具；CLI 无法观察 worker 或模型时保持 unknown。活动包、固定启动器、正在运行的 worker 和模型采用指令是四个独立状态，不能互相代替。

Windows 检查正式 0.8.25 的已登记运行包时，先核验完整文件清单和每个成员，再以该公开导出的精确清单摘要确认已审阅的 MCP 描述入口。工具 catalog 及其领域源码与当前包相同时，复用当前的纯工具声明计算。旧包中的描述入口由精确清单固定，当前 worker 的启动或租约变化不影响此计算。其它运行包沿用既有目标包描述入口。
