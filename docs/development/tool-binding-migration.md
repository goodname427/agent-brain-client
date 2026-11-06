# 工具声明与固定启动器更新

> 当前维护；适用本次分层源码的执行布局。旧版本迁移经过保存在 [历史记录](../archive/0.6/tool-binding-migration.md)。

`application/tools/catalog.py` 是内容工具名称、schema、描述和 grant 过滤的唯一来源。安装计划、只读安装检查和 MCP 的 `tools/list` 使用这一合同；日常记忆检索统一为 `memory_query`。代码组织变化不增加原 read/put/artifact/ID 或采集范围。

安装器先核验目标包的完整成员和摘要，再从它自己的 catalog 计算声明。同一实现复用已加载模块；不同目标包通过有限时、有限输出的离线描述入口读取，不能用检查器版本的猜测清单代替。此机制只接受当前完整执行布局；没有旧九项、十项清单或历史路径的兼容例外。对应测试是 `test_content_install`、`test_platform_checkpoint`、`test_architecture`。

`adapters/harnesses/codex/launcher_update.py` 负责 Mac/Windows 当前固定启动器的事务和中断恢复。只读检查不改登记；显式更新用原 policy、信任锚、解释器、身份、权限及暂停状态，保留第三方配置和原请求队列。完成重装标记的直接前驱核验用于保留事务证据，不接受旧执行代码。

`tool_discovery` 报告活动包的工具声明、登记过滤和被过滤工具。MCP 的 `client_runtime.offered_tools` 表示实际 worker 提供的工具；CLI 无法观察 worker 或模型时保持 unknown。活动包、固定启动器、正在运行的 worker 和模型采用指令是四个独立状态，不能互相代替。
