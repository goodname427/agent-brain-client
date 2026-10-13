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

`native/CredentialStore.swift` 是普通 login Keychain helper；不要求 Apple Team 或 provisioning。源码构建 helper 需要 macOS Swift 工具链；仓库安装脚本在稳定用户路径编译并自检原有 helper，复用已固定 helper；另一台新 Mac 的工具准备与真实 Keychain/UI 验收仍未完成。Swift 源码与 helper 字节哈希必须分别固定。`DeviceComponent.swift` 是另一个签名候选，不在当前批准安装范围。

已交付 HTTPS 客户端支持一个审核安装入口：先检查原生计划，再一次安全输入，prepare→安全存储→confirm 后执行原生安装；失败和重试沿用持久配对状态。支持恢复与撤销、来源隔离缓存、CAS outbox。具体交付、恢复及验收边界见 [HTTPS-INSTALL.md](docs/HTTPS-INSTALL.md)。0.2.5 已验收 HTTPS 个人内容范围，见 [PERSONAL-OWNER-SCOPE.md](docs/PERSONAL-OWNER-SCOPE.md)。本轮首次安装候选不重新验收既有 0.2.5，不发布新资产或部署生产；保留既有 SSH 恢复材料。用户资料、部署地址、私钥、配对码、原生配置和会话不进入公开仓库。

Mac arm64 的唯一用户操作说明见 [安装、使用、卸载与再次安装](docs/FIRST-INSTALL-MAC.md)，连接交付格式见 [无凭据示例](docs/connection.example.json)。同一个脚本提供 install/status/repair/uninstall；卸载移除入口和自有映射，保留身份及可恢复数据，不卸载 Codex。已有身份再次安装直接运行 `sh tools/install-mac.sh`，入口校验原绑定并复用或恢复非凭证连接描述，保留原身份、helper/profile/队列与信任锚。新设备由服务发行者用 Server 的 `tools/export_connection.py` 交付连接资料和摘要，准备完成后再走原邀请流程；没有可信交付渠道时停止，不使用示例安装。无需 Apple 分发身份或公证。独立实机验收仍须实际执行，合成验证不代表已通过。普通 Client 不包含管理员发码工具。

弱网兼容源码区分服务器本地已保存（local_durable:true、GitHub 未确认）和原 durable:true 的 GitHub 确认。前者可继续 CAS/映射，但不会冒称备份完成；同 request_id 明确重试可更新回执。公开协议见 PROTOCOL.md。HTTPS 0.2.7 通过明确 HTTP 写回执能力声明选择本地确认；运行中的旧 worker 不会热替换。0.2.5 与已冻结 0.2.6 未声明时仍保持原 GitHub 确认/可重试错误语义。Server 兼容部署不要求所有 Client 先升级；本批记忆行为与 query 的源码合入仍不代表发布。

日常保存与已有记忆迁移的默认行为见 [MEMORY-BEHAVIOR.md](docs/MEMORY-BEHAVIOR.md)：普通事实、偏好、纠正和短期进展不以重大或永久价值为门槛；Onboard 默认覆盖全部获准访问的已有记忆，排除公司敏感、秘密和范围外材料，保留来源覆盖、重复及冲突原因。`tools/memory_review.py` 只离线审阅明确提供的元数据，不采集或上传。辅助 `memory_query` 只读当前有效 Memory，最多 3 条；既有 `content_catalog` 的 Memory-only 查询别名复用同一接口。这些源码检查与合入不代表已发布、已部署或模型实测。

安装/重装成功明确交接 `onboard_check:coverage-check-required`，由 Agent 用既有 Onboard 流程核对当前来源；旧身份和旧完成标记不能证明全量，检查不触发重新配对或自动全量上传。无法观测的模型加载状态使用 `model_loaded:"unknown"`，映射数量与模型是否采用指令分开。卸载后 status 只读核验真实事务后像及登记残留，漂移明确报告 review-required。安装入口源码与运行期活动包分别交付，运行期指引和回执变化未发布前不宣称影响现有 worker。
