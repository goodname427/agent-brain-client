# 固定发布入口

> 状态：当前维护。适用：Mac / Windows 共用发布工具；执行发布仍需该动作授权。文档核对日期：2026-11-03。

`tools/release_client.py` 复用现有构建器、HTTPS 文件清单、公开扫描、隔离测试与离线 self-check。正常发布默认 `targets:all`，同时构建 Mac arm64/x86_64 和 Windows AMD64/ARM64，使用同一个组件版本、一个安装器源码归档及同一套发布／读回流程。Swift 与 Win32 凭据文件集合分别校验，各自凭据源码 pin 不变；构建不授予新的平台权限。

## 渠道与不可变版本

Windows 保留 `refs/heads/releases/personal-https-windows-stable`。Mac 保留生产登记的 `refs/heads/releases/personal-https-stable`，以及已有设备实际固定的 `refs/heads/releases/personal-https-candidate-0.4.0-install-fix-ce9f72b`；不根据名字把设备切换到别的 ref。Mac 两个既有 ref 消费同一 Mac 运行树，新提交将两个旧 tip 分别保留为直接父节点。既有锚点、政策、权限、凭据 helper 和服务器配置不变。

`auto` 从所有冻结渠道的最高版本取下一补丁。正常发布包含两个平台组；`targets:macos` 或 `windows` 仅用于补齐部分交付，仍同时构建和校验两端 manifest。显式版本必须高于所有要提升的渠道版本。安装器版本已存在时只能复用完全相同的源码、tag 和资产，不得以新 tag 为同一安装器版本换源码。

运行包 tag 分别为 `client-VERSION-windows` 和 `client-VERSION-macos`；每个平台组各有两架构归档及 SHA256SUMS。新共用安装器使用 `installer-VERSION-setup`。补齐已有 Windows 版本时复用原 `installer-VERSION-windows-setup` 及其原始资产，不重新上传 Windows 运行包或已公开的安装器。旧 tag、Release、附件和 schema-1 Windows bundle 保持可核验及可恢复。

## 正常操作

功能源码合入并通过完整 **Client checks** 后，在 main 的 **Client release** 工作流填精确源码 SHA，版本默认 `auto`、targets 默认 `all`。默认只准备；选择 `publish` 才进入独立写权限 job。局部绿色检查不能证明可发布，必要时先对最终 main 手动运行完整检查。准备 job 不持有发布凭据；发布 job 使用既有 GitHub Actions 临时身份，只申请 contents:write/actions:read，不创建 token 或环境。

工作流分别固定产品源码和执行工具的提交。工具来自本次工作流的 main SHA；产品源码可为已合入、已通过完整 CI 的历史精确 SHA，以便补齐已发布版本的另一平台。两份 checkout 均要求官方 origin、干净工作区和 Git blob 原始字节；发布前分别核验源码与执行工具的完整 CI、main 祖先关系。它不会从新工具目录把额外文件带入旧产品包。

本地等价入口（两个路径均为干净的独立官方 checkout，Python 3.11+；同一提交时可使用同一路径）：

```sh
python3 -B "$RELEASE_TOOLS/tools/release_client.py" prepare --source-root "$SOURCE_CHECKOUT" --source "$SOURCE_SHA" --targets all --output "$RELEASE_OUTPUT"
python3 -B "$RELEASE_TOOLS/tools/release_client.py" publish --source-root "$SOURCE_CHECKOUT" --output "$RELEASE_OUTPUT" --allow-publish
python3 -B "$RELEASE_TOOLS/tools/release_client.py" verify --source-root "$SOURCE_CHECKOUT" --output "$RELEASE_OUTPUT"
```

prepare 不访问网络、凭据或设备，不推送、不安装；先正常 fetch 渠道快照。它冻结源码树、执行工具、各渠道前像、版本、平台、安装器 tag 和确定性资产，运行完整隔离测试、源码／公开扫描及 diff 检查。审阅输出的 plan.json、release-notes.md 和检查日志后再发布。本地 publish 仅消费已有授权会话的 GH_TOKEN 和原 Git 推送身份；没有授权时停止，不另建凭据。

## 发布顺序与恢复

发布重新构建并逐字节比对 bundle，校验两份完整 CI 与 main 祖先关系；读取所有渠道和既有 tag／资产，原子创建不可变 tag，创建或续传 draft，核验服务端摘要／大小，公开 Release，并实际下载全部计划资产比对。之后用每个 ref 自己的前像 lease **原子提升所有选中渠道**，读回再提升共用 installer latest。未选平台的冻结渠道也须保持不变，避免部分交付把新的 installer latest 降回旧版。

完整 CI 要求同一 attempt 的分类、文档、源码、Mac、Windows 两版本、汇总和完整验证标记 jobs 全部实际成功。允许复用相同源码树的成功 PR，但仅当其 main 基线已是 head 祖先；不能把含额外基线修改的合并树结果冒充独立 head。源码须包含先前运行包记录的 Source-Commit；历史 Mac 包没有该字段时仍保留原渠道祖先，并以当前 Windows 共享源码为前进下界。

任意失败保留整个输出目录和 progress.json，重试相同 bundle；既有且一致的资产只核验，缺少的 draft 附件续传。丢失回复后先读实际状态，不删除、覆盖冲突 tag／资产，不回退其他发布。GitHub Release 与 Git refs 不是同一远端事务，中途公开资产而渠道未提升时据实报告并恢复同一 bundle。所有平台发布共用一个 concurrency 组。

Actions 重跑 publish job 复用同次 prepare artifact。重跑整个 prepare 若渠道已提升，须恢复原 bundle，不能用 auto 新版本冒充同次重试。执行工具也必须恢复 plan 固定的原提交。14 天后 artifact 可能过期，维护者应保留长期交付证据。进程强杀留下 release.lock 时先核实原进程已退出，再移除锁。

`published-and-verified` 证明 GitHub 发布、选中 Git 渠道、latest 和公开下载。生产 Server catalog/cache 由管理侧消费 plan.json 的 runtimes/manifests，沿既有受限 catalog 流程另行核验。真实设备、ARM64/x86_64 硬件、凭据 UI、Hook trust 及 worker/model 重载分别报告；没有实测不会阻止获授权的源码和包交付，也不能宣称设备验收通过。发布工具不引入 SSH 主机、部署路径、新密钥或 Server 管理入口。
