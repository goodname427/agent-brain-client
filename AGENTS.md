# Client maintenance

Client 源码、测试、构建只依赖本仓库。禁止导入 Server/Core、读取私人 Data 或把旧混合仓当构建源。协议公共实现位于 adapters/codex/content_contract.py，与 Server 的协议实现须保持兼容；跨仓验收由明确的集成测试执行。

提交采用精确文件清单。全公开源码/测试/工具先运行 publication_check，并审查路径、凭据和真实部署主机；不带入私人 Git 历史。运行 README 中测试时隔离 HOME/CODEX_HOME。构建、发布、安装是分开的动作；不改现有信任锚或能力范围来让测试通过。
