# Mac arm64：安装、使用、卸载、再次安装

支持 Apple Silicon Mac、Codex 和既有 HTTPS 个人 Memory/Rule/Skill 服务。普通客户端无管理 SSH、sudo、发码或 Server/Data 凭据。Windows 保留配置/协议边界，目前返回 unsupported，不支持实装。其他 Harness 与 Intel Mac 不在本入口范围。

## 取得源码和连接文件

在终端执行；缺少系统命令行工具时先完成 macOS 提供的标准工具安装，再重跑：

```sh
git clone https://github.com/goodname427/agent-brain-client.git
cd agent-brain-client
git rev-parse HEAD
```

管理员通过可信私有途径交付 `connection.json`，仅含 HTTPS endpoint、grant 与既有官方 release origin/ref/anchor。格式见 [connection.example.json](connection.example.json)：示例为虚构服务，不能用于真实安装；实际 anchor 必须是已批准的发布祖先，不是随意填版本 commit。连接文件没有邀请码、token、nonce、SSH 或 admin 字段。不要把真实部署配置提交到仓库。保持文件和路径稳定；以下命令将其路径保存在普通变量中：

```sh
CONNECTION_JSON="/absolute/path/to/connection.json"
```

## 安装

```sh
sh tools/install-mac.sh --connection "$CONNECTION_JSON"
```

终端展示服务器、actor、内容类型、IDs、read/put 范围及发布源；输入 `Continue` 确认，`Cancel` 退出。缺少命令行工具时，脚本请求标准安装并退出，完成后重跑同一命令。它准备 Git HTTPS、Python 3.11+、系统 TLS 根及原有 Swift helper：工具链 Python 不足时获取固定 SHA 校验的 Python 归档，准备到用户私有目录。无需 Apple 分发身份、公证、sudo、远程脚本或更改系统信任。

准备检查成功后提示 `ready.json` 的完整路径。此时管理员才按这份回执发短期邀请；客户端不发码。收到邀请后选择 `Continue`，仅在原有安全输入窗口中输入一次。不要将邀请粘贴到终端、聊天、命令参数、环境变量或剪贴板。取消/Back 可重跑；网络失败、丢 ACK 或重启必须重跑同一命令，继续原配对尝试，不申请新邀请。只有服务端明确拒绝并经再次检查和确认，才结束错误/过期邀请尝试。

`installed:true` 表示 HTTPS 协议/认证及一个 MCP 表登记完成；打开/重启 Codex，正常新 MCP 进程加载。它不会强杀旧 worker，也不意味着已运行模型自动重读了指令。再次运行安装命令会检查已装身份/范围，不再输入邀请。profile、policy、helper、scope 或配置漂移时停止并保留现场，不自动扩权。

## 状态与实际使用

```sh
sh tools/install-mac.sh --action status
```

状态列出已装活动版本、队列各状态数量和自有映射数，不访问 Keychain、不请求 Server、不输出内容正文。它是本机完整性检查，不能替代在线使用验证。

在 Codex 明确要求通过 `agent_brain_server` 列出获授权的内容，读取一个 Memory 的 revision，再用该 revision 和稳定 request_id 更新用户指定的测试内容；仅 `persistence.durable:true` 确认远端持久保存。用同一个 request_id 重试未知结果，不能当作新写入。初次验收使用用户批准的测试 ID/内容，保存并恢复原值；不要擅自改真实内容、激活 Rule/Skill 或扩大权限。读取和写入采用 `content_catalog` / `content_read` / `content_put`；新增 Rule/Skill 默认 disabled，详见 [内容范围](PERSONAL-OWNER-SCOPE.md)。

同步在启动和工具调用边界发生；原生 Memory/Rule/Skill 映射完成后，已运行模型是否重新读取仍需观察。支持传输离线时读取已核验快照、持久排队和原 ID 重试；冲突保留，不能覆盖远端修改。

## 卸载

先正常退出 Codex，使本工具 MCP worker 自然退出，再执行：

```sh
sh tools/install-mac.sh --action uninstall
sh tools/install-mac.sh --action status
```

确认 `Continue` 后移除本工具 MCP 表、登记和哈希一致的自有 Memory/Rule/Skill 文件；只删自有指令块，保留用户指令与其他 Codex 设置。空的自有 Skill 目录移除；不递归删除目录。退役缓存的 owned/active 映射指针，使再次安装能够重建相同版本映射。`client-uninstalled` 表示入口登记及映射已移除；不能以仍存有恢复材料称已安装，也不能以仍活着的旧进程称卸载验证通过。

默认保留专用 login Keychain 身份、固定 helper/bootstrap、依赖、profile/policy、安装及卸载前后像、outbox/imports 和完整快照，用于再次安装和安全恢复。它们主要位于 `~/.config/agent-brain-client`，原有安装也可能使用自己的已登记私有路径；不会卸载 Codex、改变 Server/cloud Data、撤销服务端设备或删除未同步数据。这是明确的可恢复卸载，没有“彻底擦除身份/数据”选项。

`client-in-use`：正常退出 Codex 后重跑，不杀进程、不删 lock。`uninstall-unconfirmed-writes`：保留安装，通过原 MCP 重试原 ID 或协调冲突，队列无 pending/conflict/blocked 后重跑。未知队列、未完成导入、用户修改的自有文件或 Skill 目录新增私有文件会阻断移除，保持现场；不会盲删。

## 中断恢复与再次安装

```sh
sh tools/install-mac.sh --action repair
sh tools/install-mac.sh --connection "$CONNECTION_JSON"
```

repair 继续自身中断的卸载，只接受事务前后像，外部修改停止并保留双方。安装/配对/再次安装中断重跑安装命令；它复用已保存计划、原 identity/profile/helper 和同一缓存，按现有安装事务恢复。再次安装仅准备经验证的新固定启动器和精确 MCP 计划，保留原发布政策及信任锚，不另建身份，不发新邀请。在线认证通过后重建映射，再启动 Codex 验证读写。不要删除保留目录或 Keychain 项来“修复”。

## 当前实际限制

0.2.5 活动运行包沿用受信 HTTPS 渠道，main 提供可审核的安装/生命周期源码，不直接替代活动发布渠道。自动更新在 MCP 启动时校验并激活兼容包、健康失败回滚，不自动更新固定 helper/bootstrap；固定入口更新通过明确的再次安装事务。

已确认 outbox 也占累计 1024 条上限，目前没有自动归档；达到上限会拒绝新请求，保留旧数据。Server 本地提交后 GitHub 推送未确认，会阻塞后续新写；当前 `storage_unowned` 也可能阻断缓存读取，须恢复原请求。256 条 catalog / 512 KiB snapshot 限制仍在。此版本不承诺无限写或弱网下始终可读。

源码和合成隔离测试不是独立实机验收。真实当前设备卸载/固定入口切换、Keychain、新邀请与发布渠道操作须先确认具体对象及影响；不要自动操作。安装成功后的实际 MCP 读写、卸载、再次安装由未参与开发的执行者仅按本文验证并记录版本、状态和数据保留。未实测另一台裸 Mac，不宣称完成裸机、签名、公证或 Gatekeeper 验收。
