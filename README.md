# Agent Brain Client

当前核对基线为安装器与 Windows 运行包 **0.5.9**（2026-11-03）。`main` 源码提交 `c898f4f52b1194c2d9c2ea9cbd846af820e4adb8` 对应既有安装器 tag；Windows stable 运行提交为 `db623a805e726e8f1cbc980254c56f3a45bb1e4e`。用户已确认本轮 Windows 安装与验收通过，具体证据边界见 [0.5 交付状态](docs/releases/0.5-status.md)。0.5 仍处于封版准备，本文不宣布封版。

把本仓库链接 `https://github.com/goodname427/agent-brain-client` 发给本地 Codex，即可作为 Agent Brain 安装／更新入口，无需额外安装提示。执行 Agent 先读 [统一引导](BOOTSTRAP.md)：识别实际 Windows／Mac，检测安装状态；已有安装沿原策略检查并升级，明确未安装时才首次接入。用户明确要求阅读或开发时遵循其目的。

首次只需用户选择的 HTTPS Server 地址和本机隐藏输入的 Server 长期接入密钥；安装器获取平台授权配置并完成原安装事务。密钥不得进入聊天、参数、环境变量或文件，采集目录须单独授权。状态异常时保留身份、事务与队列，不能重置身份来恢复。详细流程见 [地址与密钥安装](docs/installation/address-key-install.md)。

- [Windows 安装、升级与卸载](docs/installation/windows.md)；[安装阶段与宿主交接](docs/installation/windows-stages.md)。
- [Mac arm64 安装、使用、卸载与再次安装](docs/installation/mac.md)。
- [完整文档索引](docs/README.md)；[维护与命名规则](docs/maintenance.md)。

本仓库是公开 Client 的独立源码权威。源码、测试与构建只依赖本仓库，不导入私人 Server/Core，不克隆 Data 或读取原生会话数据库。运行时通过获准 HTTP/HTTPS 或固定 SSH 协议访问 Server。`main`、安装器 tag、产品编号和本地 `releases/manifest.json` 均不能代替设备原先固定的受信运行渠道；源码合入不自动发布或修改安装指针，Mac 既有渠道不因 Windows 0.5.9 自动提升。

## 安装与更新合同

每次从平台步骤指定的独立、干净官方 `main` checkout 启动；不从用户旧 checkout 执行。先运行平台入口的 `status`；已有核验安装使用 `check-update` 再 `update`，重复 `install` 只核验本地登记。Windows：

```powershell
& ./tools/install-windows.ps1 -Action status
& ./tools/install-windows.ps1 -Action check-update
& ./tools/install-windows.ps1 -Action update
```

Mac 从 [完整 checkout 步骤](docs/installation/mac.md#取得源码并连接-server) 取得并记录源码 SHA 后执行：

```sh
sh "$CHECKOUT_ROOT/tools/install-mac.sh" --action status
sh "$CHECKOUT_ROOT/tools/install-mac.sh" --action check-update
sh "$CHECKOUT_ROOT/tools/install-mac.sh" --action update
```

更新复用原 policy、凭据、信任锚、范围、依赖和队列，失败保留或恢复前版。分别报告 `active_package_version`（已验证活动包）、`latest_channel_version`（此次成功检查的候选）与 MCP 的 `client_runtime.running_worker_version`（实际 worker）；CLI 无法观察 worker 时相关字段为 null。检查失败时最新版未知。更新活动包后按宿主正常方式重载本服务；不强杀 worker，也无周期后台更新或热替换。

安装／重装成功的 `onboard_check:coverage-check-required` 仅交接当前 `agent-brain-onboard` Skill；不证明覆盖完成，也不批准扫描或上传。模型加载不可观测时报告 `model_loaded:"unknown"`。完整目录使用 `content_catalog` 的 `kinds`，省略 query/since/until/limit；Memory-only 查询别名与 `memory_query` 最多三条，不能证明全量覆盖。每轮记录实际版本、目录／读取次数和结果，历史超时单列，见 [验收模板](docs/acceptance/mac-install-onboard.md)。

日常保存／检索由当前加载的 `agent-brain-memory` Skill 定义；Client 工具、范围和回执语义见 [记忆接口](docs/reference/memory-behavior.md)，项目记忆入口见 [AUTOMATIC_MEMORY.md](AUTOMATIC_MEMORY.md)。本文档整理不变更记忆内容、组织或生产 catalog。新 Rule/Skill 默认 disabled，owner 范围见 [合同参考](docs/reference/personal-owner-scope.md)。`local_durable:true` 与 GitHub 确认 `durable:true` 分别报告，详见 [协议](PROTOCOL.md)。

## 开发与验证

Python 3.11+、Git；源码测试使用标准库与合成资料。隔离 HOME/CODEX_HOME，不使用当前设备的原生配置或凭据：

```sh
TEST_ROOT=$(mktemp -d)
mkdir -p "$TEST_ROOT/home" "$TEST_ROOT/codex"
export HOME="$TEST_ROOT/home" CODEX_HOME="$TEST_ROOT/codex"
PYTHONPATH=.:scripts:adapters/codex:tools:tests python3 -B -m unittest discover -s tests
python3 -B tools/check_docs.py
python3 -B tools/build_client.py --scan
python3 -B tools/check_source.py
```

构建、发布、安装分别授权和执行。构建不推送渠道、不安装、不访问凭据。包文件集合和 manifest 合同保持不变；`export-manifest.json` 是安装校验元数据，名称不代表混合仓导出。普通 Mac login Keychain helper 不要求 Apple Team／provisioning；Swift helper 需要 macOS 工具链，`DeviceComponent.swift` 仍是另一个签名候选。新设备的真实依赖、凭据 UI、宿主重载和模型采用指令必须按设备验证，合成测试不能替代。

- [多平台架构与 Harness 边界](docs/reference/multiplatform-development.md)。
- [开发检查与候选构建](docs/development/README.md)；[固定发布入口](docs/development/release-pipeline.md)。
- [历史版本与阶段](docs/archive/README.md)；既有 0.4 阶段不可变映射仍在 [历史冻结文件](FREEZE-4.0.json)，不是 0.5 封版清单。
