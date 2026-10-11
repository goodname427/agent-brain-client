# Agent Brain Client

本仓库是公开 Client 的独立源码权威。`main` 保存源码、测试与构建工具；现有受信发布渠道和 0.2.1 保持有效，迁移源码不会自动修改安装或发布指针。运行时只通过已批准的 HTTP/HTTPS 或固定 SSH 协议访问 Server；不导入 Server/Core，不克隆私人 Data。

Python 3.11+ 与 Git 是现有 Python 客户端的运行依赖。源码测试使用 Python 标准库；每个测试仅使用合成资料，HOME/CODEX_HOME 必须隔离。

```sh
PYTHONPATH=scripts:adapters/codex:tools:tests python3 -B -m unittest discover -s tests
python3 -B tools/build_client.py --scan
python3 -B tools/build_client.py --output .local/ssh-release --mode personal-content-ssh --version 0.2.1
python3 -B tools/build_client.py --output .local/https-candidate --mode personal-content-https --version 0.2.2
python3 -B tools/build_credential_helper.py --output .local/credential-helper
```

构建只读取本仓库文件，拒绝外部 Core 源根。输出保留现有 19 文件 SSH / 21 文件 HTTPS 包与 manifest 格式；构建不推送渠道、不安装、不写凭据。`export-manifest.json` 是兼容的安装校验元数据，名称不代表从混合仓导出。

`native/CredentialStore.swift` 是普通 login Keychain helper；不要求 Apple Team 或 provisioning。源码构建 helper 需要 macOS Swift 工具链；最终普通用户安装应接收已审核的二进制，目前还没有完成二进制交付和真实 Keychain/UI 安装验收。Swift 源码与 helper 字节哈希必须分别固定。`DeviceComponent.swift` 是另一个签名候选，不在当前批准安装范围。

HTTPS 候选支持 prepare→安全存储→confirm，恢复与撤销、来源隔离缓存、CAS outbox。生产 HTTPS 业务切换暂停，保留现有 SSH 客户端通道。用户资料、部署地址、私钥、配对码、原生配置和会话不进入公开仓库。
