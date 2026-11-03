# 开发与发布维护

> 当前维护；Client 源码独立构建。安装器／Windows 核对基线 0.5.9，Mac 运行渠道另行固定。

先读根目录 [AGENTS](../../AGENTS.md) 和 [多平台开发](../reference/multiplatform-development.md)。测试只使用合成资料；不跨仓导入 Server/Core，不读取生产 Data、配置、凭据或聊天会话。

## 隔离检查

```sh
TEST_ROOT=$(mktemp -d)
mkdir -p "$TEST_ROOT/home" "$TEST_ROOT/codex"
export HOME="$TEST_ROOT/home" CODEX_HOME="$TEST_ROOT/codex"
export PYTHONPATH=.:scripts:adapters/codex:tools:tests
python3 -B -m unittest discover -s tests
python3 -B tools/check_docs.py
python3 -B tools/build_client.py --scan
python3 -B tools/check_source.py
sh -n tools/install-mac.sh
git diff --check
```

Windows CI 同时隔离 USERPROFILE；原生 API／凭据／DACL 测试必须在 Windows 执行。普通提交按 [CI 变更范围表](ci-scopes.md) 选择检查：纯 Markdown 不触发 Mac／Windows 全盘测试，公共／未知改动和手动运行全量。具体平台、lint、类型检查以 [Client checks](../../.github/workflows/client-checks.yml) 为准；平台跳过须分别报告，不作为通过的实测项。

## 本地候选构建

在新的输出目录构建，版本只是未发布候选标识；构建不安装、发布或提升渠道：

```sh
python3 -B tools/build_client.py --output .local/https-mac-candidate --mode personal-content-https --platform darwin-arm64 --version 0.5.9
python3 -B tools/build_client.py --output .local/https-windows-candidate --mode personal-content-https --platform win32-amd64 --version 0.5.9
python3 -B tools/build_credential_helper.py --output .local/credential-helper
```

Swift helper 构建仅适用 Mac 工具链。SSH 历史兼容候选可使用 `--mode personal-content-ssh` 与其独立版本／政策；不能把本地候选代替既有发布源。文件范围、能力、manifest 与凭据源码 pin 均沿现有合同校验。

正式发布只使用 [固定发布入口](release-pipeline.md) 与 [发布工作流](../../.github/workflows/client-release.yml)，按最终精确提交、CI 证据和明确发布授权执行。本次文档整理不运行发布、不创建封版 tag、不提升渠道。提交使用精确文件清单，遵守 [文档维护规则](../maintenance.md)。
