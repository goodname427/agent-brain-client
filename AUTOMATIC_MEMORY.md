# 项目记忆入口

已有 Client HTTPS 安装和 read/put Memory 授权可直接复用，不需要新的 key。所有命令都在这个 Client 源码目录执行，复用安装时准备的 Python；入口不会安装解释器。项目配置和代码摘要保存在用户私有配置目录。

## 先使用已有 Memory

```sh
sh tools/memory.sh enable --project-root /absolute/project
sh tools/memory.sh status --project-root /absolute/project
```

`enable` 只登记该项目的 SessionStart 召回。它不会采集聊天、开启提取或调用模型；默认取最近更新的最多三条已授权 Memory，可用 `setup --query` 指定固定检索词。支持官方 Hooks 的 Codex 宿主在正常信任该项目 Hook 后，新会话将收到有大小上限的历史记忆上下文。安装命令不会修改宿主信任，也不能证明当前宿主已触发 Hook。

若宿主没有这些 Hook，固定使用以下命令，把返回的 `additionalContext` 交给当前会话即可：

```sh
sh tools/memory.sh recall --project-root /absolute/project
```

服务不可达时仅使用现有 Client 验证通过的缓存快照，查询结果标记 `cached_snapshot`；没有获准快照、身份/权限变更或协议错误时停止。缓存检索不保证联网查询的更新顺序。它不会从聊天历史补数据。

```sh
sh tools/memory.sh stop --project-root /absolute/project
```

`stop` 停止本项目此入口的召回和采集。再次显式 `enable` 恢复本地入口，但不能续期来源/费用窗口。软件源码更新后，重新 `enable` 更新摘要和自身 Hook 定义；其他 Hook 被保留。

## 从聊天提取，再在独立会话召回

Server 的 `setup_automatic_memory.py` 根据一份集中批准的 recipe，生成完整 Server/Client 策略和摘要。recipe 绑定来源项目、真实且仍有效的 source/recall 会话、原接收端和设备、用途、有效窗口、既有账本及费用授权。它不会猜测或伪造会话绑定，也不会清零旧额度。

把编译输出的三个 `client-*.json` 文件及 `activation.json` 以私有文件方式带到客户端，使用编译输出的真实摘要：

```sh
sh tools/memory.sh import-bundle --activation /private/bundle/activation.json --activation-sha256 REVIEWED_SHA256
sh tools/memory.sh enable --project-root /absolute/project --server-bundle /private/client-policies/client-bundle.json --server-bundle-sha256 REVIEWED_BUNDLE_SHA256
```

`import-bundle` 输出实际 bundle 路径和摘要；不需要手改策略文件。如果该项目已有只读配置，使用 `setup --project-root ... --output /private/new-settings.json --server-bundle ... --server-bundle-sha256 ...` 后，按输出的 `next_command` 安装，保留旧配置。

两种实际入口：

- `codex-hooks`：支持宿主按原生 UserPromptSubmit/Stop 的同 turn 文本配对采集，复用官方 Client outbox 和 Server committed 事件触发提取。只接收策略中指定的会话，缺 turn、缺 assistant 输出、递归 Stop 或范围不符时停止，不读取 transcript、数据库或会话目录。
- `operator-notes`：宿主不能提供 Hook 时，由用户把本次要记住的一对文本放入私有 JSON，字段为 `session_id`、`turn_id`、`user`、`assistant`。执行 `sh tools/memory.sh record --settings SETTINGS --settings-sha256 SHA --input /private/note.json`。这是显式提供笔记并触发同一提取链，不声称是原生采集。同一 turn 的重试保留原事件和请求 ID，内容改变时停止。

随后在已绑定的独立 recall 会话自动 SessionStart 读取，或执行 `recall --settings SETTINGS --settings-sha256 SHA --session-id RECALL_SESSION`。此路径只返回获准来源、有 generation 和 committed receipt 的 L1/L2/L3 派生内容，不返回 L0；不要求恰好两批。模型提取可能没有耐久事实，不制造空事实，也不自动重试未确认模型调用。

这套有限来源配置最多一小时、16 个来源和 16 个独立召回会话，受已批准预算进一步限制；不是“所有今后聊天”授权。普通 Data Memory 可在已授权设备间共享；这个有限来源工具的派生召回绑定指定设备和会话，不代表自动开放所有设备。派生内容不会自动变成 Data Memory 条目。

当前源码提供上述入口；真实 Hook 交付、模型效果及正在运行的旧 MCP worker 是否加载新版，均不能由静态构建结果推断。
