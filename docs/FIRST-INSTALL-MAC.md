# Mac arm64 仓库脚本首次安装

沿用已验收的 0.2.5、现有安全输入、HTTPS prepare/confirm、系统凭据存储、安装事务和 MCP 启动时 updater。普通 Client 没有管理 SSH、sudo、发码或 Server/Data 凭据。本入口不使用 `.app` 分发链路，也不要求 Developer ID、公证或 Apple Team。

从已审核的本仓库源码运行：

```sh
sh tools/install-mac.sh --connection "$CONNECTION_JSON"
```

`CONNECTION_JSON` 是无凭据 JSON，仅含 `schema: 1`、HTTPS `endpoint`、精确 `grant` 和 `release`。grant 为已有 Codex / darwin-arm64 / read、put 范围。release 固定官方公开 Client origin、`refs/heads/releases/personal-https-stable` 和已审核 40 位 anchor；不接受 token、邀请码、nonce、SSH、admin 或任意发布源。连接配置通过可信私有途径交付，真实部署地址不进入公开源码。

脚本准备实际依赖：缺少系统命令行工具时请求标准工具安装，结束后重跑同一命令；这些工具提供 Git HTTPS 和编译原有 Swift helper 的能力，不需要创建 Apple 分发身份。没有调用 sudo、包管理器或任意远程脚本。Bootstrap 使用工具链 Python 运行；不足 3.11 时，从固定 [上游归档](https://github.com/astral-sh/python-build-standalone/releases/tag/20261009) 获取 Python 3.12.15 arm64，先校验源码内固定 SHA-256，再限制归档路径/链接/大小并准备到用户私有目录。网络失败保留暂存且不执行未验证下载，重跑可以重新准备；已准备的解释器复用。不复制整个 app 或 Git/Python 依赖树，不在安装器中重复启动自己。

入口在终端展示服务器、actor、授权 IDs、read/put 范围及发布源/anchor，用户选择 Continue 后检查 Python/TLS、Git HTTPS，编译并自检原有 helper（已有同一 helper 复用），自动生成私有 profile、内容政策、发布政策与原生安装计划。固定启动器只准备既有 21 文件运行包；中断输出保留，重试在完成后原子放入固定目录。现有安装要求自己的精确迁移计划，首次安装不会替换它。

Helper 使用普通 login Keychain、专用 service 与固定 executable 访问控制；安全输入是已有 `NSSecureTextField`。必须保持设备本地稳定路径、helper 字节与 Swift 源码摘要；不能随升级重新编译并悄悄更换已绑定 helper。源码构建需要 Swift 工具链，这是当前源码安装路线的真实依赖。没有改 helper 源码、Keychain ACL、TLS 信任锚或设备能力范围。

依赖、自检、受信渠道健康和原生计划全部通过后才生成最多十分钟的 `ready.json`；此时才申请短期邀请。schema 2 回执有稳定的非敏感 installation_id，同一准备目录跨重启/重新准备复用，绑定前像保留；它不是 Server 设备身份。`bundle_sha256` 字段保留原名，当前固定准备材料元数据，不要求 `.app`。私有发码账本按该安装实例关联原回执，不以刷新时间或更换回执当作未知结果已结束；旧无关联账本必须先核对，不能自动删除/等待过期后重发。管理员复用独立 Server 仓库的既有 issuer 能力和私有交付，Client 不发码。Continue 才调用 `install_once` 的一次安全输入；所有敏感内容仅在安全输入、专用保护项及私有管道中流转，不经终端 stdin、聊天、剪贴板、argv、环境或日志。

取消/Back/重复启动不改原生登记。错误或过期邀请码只有在服务端明确 prepare 拒绝、重放同一 code/nonce 确认并经用户确认后，才清除该 preparing 记录并结束尝试。网络失败、丢 ACK、未知回复和重启复用原 code/nonce 或 pending/active 凭据，不清空、不自动发码或新建身份。非敏感尝试标记只控制“恢复既有尝试”的提示与 readiness：已开始的尝试不刷新可发码 ready，配对状态仍由原有系统保护项管理。旧 ready 存为历史，失败重检不会继续提供当前 ready。依赖、helper、连接及原生配置漂移保留并停止；安装中断沿用现有前后像恢复。既有快照、队列及更新回滚保持原机制。

依赖使用明确可执行路径和私有策略，MCP/probe 固定环境并禁止 Git 全局凭据与交互提示。新增 schema 2 记录本地 Python/Git 的路径及 executable 摘要；旧 schema 1 完整依赖树策略仍可校验，旧无依赖策略保持原行为。系统工具或解释器更新造成绑定漂移时需要审核，不覆盖旧计划或凭据。

Windows 从现在按平台边界设计：共享配对恢复/事务/updater 与 store/input 注入接口复用，配置生成/验证复用同一入口，Windows 凭据描述明确 unsupported、不带 Mac helper/源码字段；发布政策和安装作用域按对应纯路径语法验证，TOML 生成保持无关配置。真实权限、凭据、更新、安装恢复、缓存和 worker 操作经现有平台边界分派，Windows 在私有读写/进程/网络更新之前返回明确 platform-unsupported。契约覆盖路径、配置、拒绝越权/降级、原状态保留与无 POSIX 回退；Mac 旧配置格式保留。当前 Mac 入口、login Keychain、所有权校验、POSIX worker lease 和 Server 平台许可仍不适用于 Windows；未实现 Windows 凭据后端/可信输入/启动或实机安装。契约测试不会扩大许可或明文回退，见 [跨平台验收](PLATFORM-PAIRING-ACCEPTANCE.md)。

本轮只开发和隔离验证，没有执行系统工具安装、真实 Keychain 输入/写入、新设备发码、发布资产或生产部署。另一台新 Mac 的工具准备、真实 UI/Keychain/MCP 与重启恢复尚未实测。真实安装、发码、设备身份和发布渠道变更仍需具体对象与权限批准；签名、公证不是本路线的新增批准门槛。
