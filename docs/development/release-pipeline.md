# 固定发布入口

> 状态：当前维护。适用：0.5.9 起的现有发布工具；执行发布仍需该动作授权。文档核对日期：2026-11-03；验收范围见 [0.5 交付状态](../releases/0.5-status.md)。

`tools/release_client.py` 复用现有 `build_client.release`、HTTPS 文件清单、manifest、公开扫描和离线 self-check。本轮只接通现有 Windows AMD64／ARM64 stable，Mac 渠道不提升；没有新增 Server 管理入口或权限政策。功能开发、代码评审、用户桌面验收仍各自完成，不能将实际代码修复耗时归为发布脚本问题。

审计基线是已发布的 0.5.8：仓库原来只有 `client-checks.yml` 和单包构建工具，没有正式发布工作流。安装器全源码归档、两架构包、SHA256SUMS、运行包 Git 提交、两个 GitHub Release、latest、Windows 渠道及五个实际下载核验均由仓外临时脚本串联；脚本还嵌入当版版本、tag 和批准断言。`releases/manifest.json` 是历史包资料，不能用来推断源码版本。新入口从冻结的 Windows 渠道确定版本和兼容边界，不改它或每版增加工具清单、测试例外。

正常动作：功能 PR 合入并通过 Client checks 后，在 main 的 **Client release** 工作流填精确源码 SHA，版本默认 `auto`。默认只准备；选择 `publish` 才进入独立的写权限 job。准备 job 不持有发布凭据；发布 job 只申请 contents:write/actions:read，使用已有 GitHub Actions 临时身份。无需创建 token、环境、Server 配置或安装权限。受保护分支、仓库 Actions 政策仍由 GitHub 执行。

本地等价入口（在干净的独立官方 checkout，Python 3.11+）：

```sh
python3 -B tools/release_client.py prepare --source "$(git rev-parse HEAD)" --output .local/release
```

`prepare` 不访问网络或凭据，不推送、不安装。它锁定精确提交／源码树／渠道前像，自动取下一补丁版本（也可 `--version major.minor.patch`），核对工作树与 Git blob 原始字节，扫描全部受版本控制文件；复用构建器生成两平台包，执行离线包自检、隔离 HOME/CODEX_HOME 的完整标准库测试、源码检查和 diff 检查；输出确定性归档、五个资产、摘要、摘要说明及计划。需要更新本地渠道快照时，维护者先正常 fetch；陈旧快照不会被发布阶段悄悄覆盖。

发布前先审阅 `.local/release/plan.json`、`release-notes.md`、检查日志。获得实际发布授权后，使用既有 Git 推送身份及现有授权会话提供的 `GH_TOKEN`，执行：

```sh
python3 -B tools/release_client.py publish --output .local/release --allow-publish
python3 -B tools/release_client.py verify --output .local/release
```

固定顺序是重新按冻结源码构建比对计划／资产 → 核对源码属于远端 main → 验证精确源码树对应的成功 Client checks（允许合并提交和已通过 PR 使用相同树）→ 校验远端前像和已有资产 → 原子创建两个不可变 tag → 创建或续传 draft → 核验服务器摘要／大小 → 发布两个 Release → 实际下载比对五个资产 → 带前像 lease 提升 Windows 渠道 → 提升 installer latest → 再读 tag、渠道、latest 和公开下载。CI 依据为整个既有工作流成功，不复制每版测试数量或门禁白名单；取消或失败的运行不被描述为通过。两架构若不再共享相同运行树则明确停止，不能将其中一个包偷偷指向另一架构渠道。

PR CI 默认检查合并树。仅当该运行的 main 基线已是 head 的祖先时，才可复用其结果证明 head 的字节；基线含有分支尚未包含的修改时，等待合并后的 main CI，不能将合并树的结果冒充独立 head 已通过。

兼容门禁从当前渠道 manifest 与构建器计算：文件成员、能力、协议、状态 schema、最低 Python、迁移以及固定 Windows 凭据组件必须保持原合同。源码必须包含上一版 installer tag 的源码提交，不能只提高包版本却退回旧源码。需要变更时走真实的兼容／权限审查；不放宽安装信任锚、不改凭据摘要、不将工具列表冻结成每版手改的断言。运行渠道提交保留原提交为直接父节点，旧历史和锚仍是祖先。

失败恢复：保留整个输出目录，重跑相同命令。固定版本、源码、父提交与确定性资产不会随重试漂移；已一致的附件复用，缺少的 draft 附件续传，已公开的 release 只核验。每个远端修改之前有读回，丢失回复后先读实际状态；从不删除／覆盖冲突 tag 或资产。两个 release 和渠道不是同一远端事务：中途可能已公开资产但渠道尚未提升，`progress.json` 会保留失败阶段；此时沿同一 bundle 重试。若其他发布已推进渠道则停止，不回退或覆盖它。正常中断会释放目录锁；进程被强杀留下 `release.lock` 时，先确认原进程已退出再手工移除锁。

Actions 可重跑失败的 publish job，复用同次 prepare artifact。整次重新运行 prepare 若渠道已提升，必须恢复原 bundle（下载同次 artifact，本地运行 publish），不要重新选择 auto 版本冒充同次重试。14 天 artifact 过期后仍可用原完整 bundle；长期交付证据应由维护者自行归档。

`published-and-verified` 仅证明 GitHub 发布、Windows Git 渠道和公开下载。生产 Server 的两平台 catalog/cache、Windows 桌面、ARM64 硬件、Hook trust 和实际 worker/model 重载仍明确为未检查。这些核验不能由公开 Client 仓访问私人 Server 源码或生产身份代做；Server 管理侧应消费 `plan.json` 中的 version/runtime/manifest_sha256，通过既有受限 catalog 检查流程返回证据。本入口不会为此引入 SSH 主机、部署路径或新密钥。首次真实授权发布仍需实际验证 GitHub 写权限、网络下载及生产 catalog；本轮开发不能宣称这些已成功。
