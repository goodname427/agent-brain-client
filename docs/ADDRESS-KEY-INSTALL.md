# HTTPS 地址与长期接入密钥

正常首次安装只填写两个值：你选择的 Server HTTPS 地址，以及 Server 生成的长期接入密钥。地址可由本机终端询问或 `--server`（Windows `-Server`）指定；密钥只能由本机隐藏输入填写，不传给聊天或执行 Agent。Mac 使用独立 login Keychain 项，Windows 使用当前用户 Credential Manager；没有明文落盘、命令行或环境变量回退。没有本机交互能力时停止，不能改为聊天收集密钥。

从干净官方 main 运行 [Mac](FIRST-INSTALL-MAC.md) 或 [Windows](FIRST-INSTALL-WINDOWS.md) 的正常入口。执行 Agent 先检查 status：已安装就按原策略 check-update/update；已卸载就恢复原身份；状态异常时保留事务和队列。首次无参数运行会在本机询问地址和密钥。服务发行者须先启用 Server 的地址／密钥协议，并通过其既有安全渠道交付密钥；公开仓库不含实际服务地址、用户密钥或管理员入口。

## 自动完成的工作

1. 验证用户所选 HTTPS origin 的正常证书与主机名。拒绝 URL 中的凭据、路径、查询、片段与控制字符，不追随认证重定向，不使用环境代理。
2. 在 OS 凭据库保存原始密钥，向 `/v1/access/bootstrap` 的 Authorization 头发送密钥。请求只含实际 adapter/platform；认证成功才返回该平台的授权范围与官方发行 origin/ref/anchor。
3. 以该用户选择且通过 TLS 验证的 Server 为引导信任来源。Server 给出的 anchor 是明确的发行授权；随后复用原 Git 祖先／manifest／文件范围／健康检查。下载内容附带的 hash 只用于完整性和本地漂移检查，不能自行成为信任根。
4. 通过 `/v1/access/enroll` 以稳定 nonce 取得独立设备凭据，安全保存后复用原 confirm。密钥保留在按 origin 隔离的专用 OS 项，设备凭据仍在按 profile 隔离的原 OS 项。持久状态文件只有非秘密元数据，恢复标记不是接入密钥。
5. 完成原本的原生配置事务、登记和只读协议检查后才返回 `installed:true`。网络认证成功本身不是安装完成。正常重载该 MCP 后观察 worker 版本；模型加载无法观测时报告 unknown，再按现有 Onboard 检查覆盖。

这些授权仅涵盖指定个人内容 API。接入密钥不授予 OS、Server 管理或任意目录访问；不会扫描原生会话、自动启用 Hook、批准未来目录或代表 Onboard 已完成。未来采集范围继续走现有精确来源授权。

## 重试、撤销与旧设备

取消隐藏输入不会建立设备。网络／TLS／未知回复保留同一 OS 凭据、nonce 与本地事务；重跑相同入口恢复。Server 明确返回 access_denied 才移除这个被拒绝的 origin 密钥项，下次可隐藏输入正确密钥；设备身份、记忆缓存和队列仍保留。非零退出及 installed:false 必须如实报告，不能把旧安装或认证结果当成功。

密钥不设默认到期，Server 只保存随机密钥的 SHA-256 摘要。发行者可按非秘密 key_id 撤销或轮换；撤销会阻止新的引导，并使关联设备的后续认证／确认失败。轮换产生新密钥并撤销旧密钥，旧设备不会被静默改成新身份。已确认撤销而需要重新授权的设备，应保留队列后按精确设备恢复计划处理；不能删除凭据来强制新身份。

已有邀请协议的设备、旧 Mac 发布渠道、固定 helper、原 identity、outbox 和未确认请求继续保留；普通升级与再次安装尽量复用原记录，不强制全部重装。旧协议未完成的安装仍可用原审核连接资料恢复。Server 未启用新协议时会返回错误；普通用户无须取得私人 Server 仓库或离线连接包。

## 验证边界

隔离合成测试覆盖接入协议、错误密钥、长期有效、响应丢失、撤销、轮换、范围拒绝和安装事务；Mac/Windows CI 使用各自 OS 的现有凭据及权限测试。CI 不等同于真实 Windows 桌面的输入体验、Codex 重载或模型采用指令。没有新增真实模型测试、真实长期密钥或目录采集授权。

Windows 原生隐藏输入使用通用应用凭据对话框（固定 Agent Brain 用户名标签、禁用 CredUI 自行持久化），密钥随后仅由专用 Credential Manager 项保存；不是 Windows 登录／管理员密码。API 依据：[Microsoft CredUIPromptForCredentialsW](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-creduipromptforcredentialsw)。原生 UI 不可用时停止，没有终端回显回退。

安装器 0.5.2 修复正常 Windows checkout 缓存的首次路由，以及 registration 已写入但 applying 回执尚未提交的中断恢复。此阶段重跑同一入口会先核验原连接、绑定、计划和 journal 前后像，再沿原设备认证与安装事务恢复；不会重新 bootstrap、索取 legacy 文件或新密钥。外部编辑或绑定漂移会停止并保留现场。
