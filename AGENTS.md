# Client maintenance

用户将本仓库链接作为 Agent Brain 接入入口交给 Codex 时，包括只发送该链接的情形，先读 `BOOTSTRAP.md`，识别实际平台并检测安装状态；已有安装走原策略的检查／升级，未安装走对应平台首次安装。不要求用户另写安装提示。若用户明确请求阅读、开发或其他工作，遵循其明确目的。仓库链接不批准新设备凭据、采集目录或权限扩张，首次仅用用户选择的 HTTPS Server 地址与本机隐藏输入的 Server 长期接入密钥，遵循 docs/ADDRESS-KEY-INSTALL.md；旧身份复用，不索取连接文件、摘要或邀请码。

Client 源码、测试、构建只依赖本仓库。禁止导入 Server/Core、读取私人 Data 或把旧混合仓当构建源。协议公共实现位于 adapters/codex/content_contract.py，与 Server 的协议实现须保持兼容；跨仓验收由明确的集成测试执行。

提交采用精确文件清单。全公开源码/测试/工具先运行 publication_check，并审查路径、凭据和真实部署主机；不带入私人 Git 历史。运行 README 中测试时隔离 HOME/CODEX_HOME。构建、发布、安装是分开的动作；不改现有信任锚或能力范围来让测试通过。
