# Agent Brain Client

本仓库是公开 Client 的独立源码权威。`main` 保存源码、测试与构建工具；现有受信发布渠道和已验收的 0.2.5 保持有效，迁移源码不会自动修改安装或发布指针。运行时只通过已批准的 HTTP/HTTPS 或固定 SSH 协议访问 Server；不导入 Server/Core，不克隆私人 Data。

Python 3.11+ 与 Git 是现有 Python 客户端的运行依赖。源码测试使用 Python 标准库；每个测试仅使用合成资料，HOME/CODEX_HOME 必须隔离。以下测试命令先准备临时 HOME/CODEX_HOME；不使用当前设备的原生配置或凭据。

```sh
TEST_ROOT=$(mktemp -d)
mkdir -p "$TEST_ROOT/home" "$TEST_ROOT/codex"
export HOME="$TEST_ROOT/home" CODEX_HOME="$TEST_ROOT/codex"
PYTHONPATH=scripts:adapters/codex:tools:tests python3 -B -m unittest discover -s tests
python3 -B tools/build_client.py --scan
python3 -B tools/check_source.py
python3 -B tools/build_client.py --output .local/ssh-release --mode personal-content-ssh --version 0.2.1
python3 -B tools/build_client.py --output .local/https-candidate --mode personal-content-https --version 0.2.6
python3 -B tools/build_credential_helper.py --output .local/credential-helper
```

构建只读取本仓库文件，拒绝外部 Core 源根。输出保留现有 19 文件 SSH / 21 文件 HTTPS 包与 manifest 格式；构建不推送渠道、不安装、不写凭据。`export-manifest.json` 是兼容的安装校验元数据，名称不代表从混合仓导出。

0.2.4 修复原生配置所有权与计划渲染：运行期只校验本客户端的 MCP 表及登记、包和策略绑定，Codex 模型及其他无关设置可正常变化。安装事务仍保存整文件前后像，只恢复匹配事务后像的文件，保留外部修改。计划保存/读取后的渲染摘要保持一致。升级必须同时覆盖原生固定启动器及其实际导入的依赖；只更新活动包不能修复旧固定入口。旧发布包不原地改写。

`native/CredentialStore.swift` 是普通 login Keychain helper；不要求 Apple Team 或 provisioning。源码构建 helper 需要 macOS Swift 工具链；最终普通用户安装应接收已审核的二进制，首次安装构建器会编译并打包该 helper；另一台裸 Mac 的二进制交付与真实 Keychain/UI 验收仍未完成。Swift 源码与 helper 字节哈希必须分别固定。`DeviceComponent.swift` 是另一个签名候选，不在当前批准安装范围。

已交付 HTTPS 客户端支持一个审核安装入口：先检查原生计划，再一次安全输入，prepare→安全存储→confirm 后执行原生安装；失败和重试沿用持久配对状态。支持恢复与撤销、来源隔离缓存、CAS outbox。具体交付、恢复及验收边界见 [HTTPS-INSTALL.md](docs/HTTPS-INSTALL.md)。0.2.5 已验收 HTTPS 个人内容范围，见 [PERSONAL-OWNER-SCOPE.md](docs/PERSONAL-OWNER-SCOPE.md)。本轮首次安装候选不重新验收既有 0.2.5，不发布新资产或部署生产；保留既有 SSH 恢复材料。用户资料、部署地址、私钥、配对码、原生配置和会话不进入公开仓库。

Mac arm64 首次安装入口、自动依赖与配置准备、明确拒绝邀请码的恢复以及发布前缺口见 [FIRST-INSTALL-MAC.md](docs/FIRST-INSTALL-MAC.md)。构建和模拟验证不代表安装包已正式发布、签名或公证。普通 Client 不包含管理员发码工具。
