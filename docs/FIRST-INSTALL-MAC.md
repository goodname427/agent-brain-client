# Mac arm64 首次安装 MVP

源码开发基线为已验收的 Client 0.2.5。此入口是未发布安装候选：本轮只开发、模拟和隔离验证，没有实际给新设备发码、建立身份、写入真实 Keychain 或更改现有安装。已运行的 worker 不停止；现有 MCP 启动时更新、校验、激活和健康失败回滚继续沿用。

最终用户打开 `Agent Brain Install.app`，选择维护者通过私有途径提供的无凭据连接 JSON，确认服务器、actor、Memory/Rule/Skill 范围、公开发布源和固定 anchor。向导把随包审核的 Python 3.11+、Git HTTPS 运行时、原有 login Keychain helper 和固定入口复制到用户目录，自动生成 0600 profile、内容政策、发布政策与原生安装计划。没有 Homebrew、sudo、Xcode 安装、任意远程脚本或模型调用。

依赖复制可以中断重试；现有同字节文件复用，漂移文件保留并停止。运行时依赖树和链接目标固定摘要；只允许树内相对链接。MCP 计划固定新的依赖 PATH / Git helper 路径，禁止 Git 的全局凭据配置及交互提示。旧安装没有这些新增字段，保持原计划。已有登记需要自己的迁移计划，不能由首次安装替换。

配置只接受 `schema: 1`、HTTPS `endpoint`、精确 `grant` 和 `release`。`grant` 沿用现有 Codex / darwin-arm64 / read、put 范围；`release` 只允许既有官方公开 Client origin、`refs/heads/releases/personal-https-stable` 与审核的 40 位 anchor。邀请码、nonce、token、SSH 或 admin 字段不被接受。安装器不会修改已有信任锚或扩大能力来接受候选。

兼容版本必须从该受信发布 ref 通过原有 updater 校验后可用。只构建了候选、还没有发布兼容版本时，向导会停止，不能绕过此检查或把本地候选冒充受信版本。依赖、helper、发布健康和原生计划全部准备好后，向导生成有效期至多十分钟的私有 `ready.json`，展示“现在可以向管理员申请短期邀请码”。私有管理员控制台仅存在独立 Server 仓库，普通 Client 不具有发码或管理权限。

安全输入和配对复用 `https_content.install` 的现有事务。真正的敏感输入只进入可信本地安全文本框、专用 Keychain 项及私有 helper 管道，不进入聊天、剪贴板、argv、环境或日志。确认后才执行原生配置写入与前后协议检查。

取消或返回不会改原生登记。重复启动由持久锁阻止重复提示和身份创建。网络失败、丢 ACK、重启均保留原邀请码与 nonce 或 pending/active 凭据；重试不再次申请授权。服务器明确返回 prepare `pairing_denied` 时，向导重新验证同一输入终态并让用户确认清除该 preparing 记录，然后结束此次尝试；不会自动发码或立即再次配对。错误和过期输入在服务端统一拒绝，界面不泄露邀请码有效性。unknown/5xx/401/重定向不能触发清除。pending/active 的现有过期、撤销恢复保持原边界。

已安装后重复入口只检查已批准事务；中断的原生事务按前后像恢复。原生配置、profile、release origin、helper 或依赖树漂移都会停止，保留外部编辑、快照、队列和恢复材料。所有返回值和错误均为固定状态，没有原始异常或 HTTP 内容。

构建由维护者明确提供两个可重定位依赖目录。Python 前缀须有 `bin/python3`、标准库、TLS 支持；Git 前缀须有 `bin/git`、`libexec/git-core/git-remote-https` 与所需动态库。Python 使用 macOS 维护的 TLS 根证书；不能通过关闭校验替代。provenance JSON 固定 `schema: 1`、`platform: darwin-arm64`，以及 `python` / `git` 各自的公开无凭据 `source`、`archive_sha256` 和随包 `license_files`。完整依赖和许可证不进入源码 Git 历史。

```sh
python3 -I -B tools/build_mac_installer.py \
  --output "$CANDIDATE_APP" --python "$REVIEWED_PYTHON_PREFIX" \
  --git "$REVIEWED_GIT_PREFIX" --provenance "$DEPENDENCY_PROVENANCE"
```

构建器检查可重定位 Python 的版本、arm64、TLS 根，Git HTTPS helper、原有 helper 自检和完整字节清单。依赖由维护者审核后随包配送，用户端自动准备。本轮已构建包含真实 arm64 Python 3.12.15、Git 2.56.0 和原有 helper 的未发布候选，并进行隔离重定位检查；依赖来源及许可证文件随包保留。正式分发的依赖许可证/对应源码审查、签名、公证、Gatekeeper 下载来源验证、另一台裸 Mac 的真实 UI/Keychain/MCP 加载验收仍未完成。不要移除 quarantine、绕过系统安全或创建 Apple 凭据。实际资产发布和新设备授权需要具体批准。
