# Mac 首次安装开发验收

开发从 Client main `62a6f001` 和独立 Server main `7cda6eb7` 继续；已交付 0.2.5 不重新部署。公开源码只依赖本 Client，私有发码工具只在 Server；没有读取生产 Data、原生会话数据库或 sessions，也没有停止旧 worker。

Client 全量隔离测试 143 项通过，包含新首装取消、返回、并发重复启动、错误输入、统一 prepare 拒绝、网络失败、丢 ACK、依赖复制中断、重复安装、原生配置/依赖/连接漂移。既有队列、快照、CAS、生命周期、启动更新和健康回滚测试全部通过。Server 全量隔离测试 147 项通过；最新私有控制台 8 项通过，验证一次发码模拟、取消、新鲜度、范围/摘要漂移、丢 ACK 持久阻止重发和私有 UI 管道。最终 PR 的 CI 在对应提交上再运行完整检查。

新增工具通过 Ruff 0.14.0 与 mypy 1.18.2 的函数体检查；所有 Client Python 源码通过无字节码语法检查。公开源码 publication_check 通过，未放入真实端点、个人路径、密钥、运行配置或 Git 私人历史。额外候选全字节扫描仅对与已验证官方 Python 归档完全相同的上游文件产生通用 IP/版本数字示例匹配，没有私人路径、密钥或项目文件匹配；未修改扫描规则或上游字节。这些第三方运行时不进入源码仓库。

真实未发布 arm64 app 已构建，随包 Python 3.12.15、固定官方 Git 2.56.0 源码构建的 Git HTTPS helper、原有 login Keychain helper 和完整许可证文件。helper 构建目标为 macOS 13，自检不访问 Keychain/UI。候选 1710 项清单固定 SHA-256；隔离 HOME 重定位后 Python/TLS 根、Git init/hash、自检及 Client MCP self-check 全部通过，无网络、身份、凭据或原生配置变更。Keychain helper 的 Swift 源码及既有信任摘要保持一致，HTTPS 更新协议仍为 21 文件。

仍未完成：正式依赖许可证/对应源码分发审核、Developer ID 签名、公证、Gatekeeper 下载来源验证、另一台真正裸 Mac 的 UI/Keychain/MCP 加载验收。没有发布资产、实际发邀请码、创建新设备身份、配置持久凭据或部署生产。真实发布、发码和授权仍须确认具体设备、范围、有效期及权限；缺失的验收不能由合成测试代替。
