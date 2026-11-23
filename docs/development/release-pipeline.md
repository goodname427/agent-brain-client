# 固定发布入口

> 状态：当前维护。适用：Mac / Windows 共用发布工具；执行发布仍需该动作授权。文档核对日期：2026-11-04。

`dev/release/client.py` 复用现有构建器、HTTPS 文件清单、公开扫描、隔离测试与离线 self-check。正常发布默认 `targets:all`，同时构建 Mac arm64/x86_64 和 Windows AMD64/ARM64，使用同一个组件版本、一个安装器源码归档及同一套发布／读回流程。Swift 与 Win32 凭据文件集合分别校验，各自凭据源码 pin 不变；构建不授予新的平台权限。

## 渠道与不可变版本

Windows 保留 `refs/heads/releases/personal-https-windows-stable`。Mac 保留生产登记的 `refs/heads/releases/personal-https-stable`，以及已有设备实际固定的 `refs/heads/releases/personal-https-candidate-0.4.0-install-fix-ce9f72b`；不根据名字把设备切换到别的 ref。Mac 两个既有 ref 消费同一 Mac 运行树，新提交将两个旧 tip 分别保留为直接父节点。既有锚点、政策、权限、凭据 helper 和服务器配置不变。

`auto` 从所有冻结渠道的最高版本取下一补丁。新版本总是发布两个平台组。单平台目标仅用于补齐已发生的历史部分交付，要求另一组所有冻结渠道已是同一版本且存在对应的原始安装器。补齐时仍构建并校验两端 manifest。安装器版本已存在时只能复用完全相同的源码、tag 和资产，不得以新 tag 为同一安装器版本换源码。

运行包 tag 分别为 `client-VERSION-windows` 和 `client-VERSION-macos`；每个平台组各有两架构归档及 SHA256SUMS。新共用安装器使用 `installer-VERSION-setup`。补齐已有 Windows 版本时复用原 `installer-VERSION-windows-setup` 及其原始资产，不重新上传 Windows 运行包或已公开的安装器。旧 tag、Release、附件和 schema-1 Windows bundle 保持可核验及可恢复。

## 正常操作

源码通过正常 PR 审阅合入后，在 Mac 或 Windows 上运行正式入口。默认 `publish` 直接执行一次正式快检，校验绑定最终候选源码树的结果，然后使用既有授权执行发布。四架构打包、公开扫描、manifest／源码字节核验与包 self-check 属于产物准备；上传和公开下载读回也单独计时。

产品源码和执行工具分别固定精确提交，均来自官方仓库、已合入 main、工作区干净且字节等于 Git blob。若两份 checkout 不同，正式测试在产品 checkout 上运行。执行工具按其原有身份校验，不向旧产品包额外打入文件。官方 `Client release` 手动入口保留独立的只读准备 job 与 `contents:write` 发布 job。现有工作流的发布命令兼容旧参数 `--wait-for-ci 1800`，按默认入口运行本次快检，旧参数不查询或等待 GitHub CI。

HTTPS 构建从精确产品提交的 Git 树选取 Python 模块。已核验的安装器源码归档使用归档内的源码树。两种入口共用平台文件范围和 Python 布局校验。合法范围内普通模块的新增、移动和删除随提交进入清单。已有注册策略的摘要、固定入口、凭据源码 pin 和包成员仍按各自的信任来源验证。

本次从 0.8.29 升级的候选保留现有 Python 文件路径。使用正式 0.8.29 Mac 包的隔离登记验证，路径集合相同的候选能在原 MCP 连接上读取。移动普通模块的试验中，活动包切换后，0.8.29 登记路径的校验器仍拒绝新包。原连接继续从上一版读取，普通重连仍使用同一校验器。

本地入口使用 Python 3.11+，并以既有 Git 推送权限和获授权的 `GH_TOKEN` 发布。使用 Git 外的输出目录。

```sh
python3 -B "$RELEASE_TOOLS/dev/release/client.py" prepare-local --release-notes-file "$RELEASE_NOTES_FILE" --source-root "$SOURCE_CHECKOUT" --source "$SOURCE_SHA" --targets all --output "$RELEASE_OUTPUT"
python3 -B "$RELEASE_TOOLS/dev/release/client.py" publish --source-root "$SOURCE_CHECKOUT" --output "$RELEASE_OUTPUT" --allow-publish
python3 -B "$RELEASE_TOOLS/dev/release/client.py" verify --source-root "$SOURCE_CHECKOUT" --output "$RELEASE_OUTPUT"
```

`prepare-local` 读取官方 main 与三个渠道前像，冻结源码、版本、渠道、包和更新日志。`prepare` 和 `prepare-artifacts` 使用本机已同步的渠道前像准备相同产物。发布前会重建并核对原包。旧 bundle 固定原执行工具身份。若新工具无法加载仍未发布的旧 bundle，保留原目录，用合入后的执行工具和原产品提交在新目录重新准备并逐件核对资产。部分上传后按原身份恢复同一 bundle。

## 每版更新日志

启动 `Client release` 时必须填写 `release_notes`；本地 `prepare` / `prepare-local` 使用 `--release-notes-file` 指向 UTF-8 Markdown 文件。内容只包含可公开的新增、修复、影响与升级／重载提示，不填写凭据、个人资料或私有日志。空白输入在构建前拒绝，正文沿用公开内容扫描。

prepare 将这份日志放在 Release 正文顶部，保留后面的源码、运行包、manifest 摘要和验收边界，并在 plan 中固定完整正文摘要。同一 bundle 的重试不得换日志；publish 不再接收另一份文本，自动将准备好的正文用于本轮新建的安装器和平台 Release。补齐平台时已有安装器 Release 不改写。检查 `release-notes.md` 后再发布；流程不会自动改写历史版本，也不会仅因日志功能自动增加产品版本。

## 正式预发快检

Mac 或 Windows 任一实际通过的同树快检可交付全部四架构及统一安装器。默认由发布器运行并直接消费实际结果。需要提前跑一次或从另一平台复用时，使用执行工具 checkout 的 `dev/checks/client.py`，将 `--source-root` 指向干净的候选产品 checkout：

```sh
python3 -B "$RELEASE_TOOLS/dev/checks/client.py" release --record --source-root "$SOURCE_CHECKOUT" --output "$CHECK_OUTPUT"
python3 -B "$RELEASE_TOOLS/dev/release/client.py" publish --source-root "$SOURCE_CHECKOUT" --output "$RELEASE_OUTPUT" --allow-publish --preflight-result "$CHECK_OUTPUT/checks.json"
```

仅接受受信操作员真实执行的快检结果、同目录日志和摘要。结果绑定候选 Git 树、执行平台、命令、退出码和总墙钟。每次追加正式用例都要在参考 Mac 重跑整套，在 PR 记录测试选项与总墙钟，超过五分钟先优化或拆出长回归。实测让全部测试自然退出，失败保留日志并阻断。

显式 `--skip-preflight-tests` 只跳过当次测试，写入 `not-run`。同一 bundle 上一次失败或中断时需重跑。各次尝试都保存在 `preflight` 子目录。发布权限、main 祖先、版本、包摘要、不可变 tag、读回和渠道 lease 在两种选项下照常执行。

#262 的参考 Mac 使用 arm64 和 Python 3.12.16。旧串行发现的 1,305 项耗时 1,974.58 秒，64 项按条件跳过，沿用 [#265](https://github.com/goodname427/agent-brain-client/pull/265) 的实测。新版默认快检覆盖文档链接、源码边界、安装与授权、版本和摘要失败、双平台渠道事务。在参考 Mac 上通过三个步骤和 10 项测试，改造分支总墙钟 15.86 秒，0.8.34 锁定产品树总墙钟 16.19 秒。另一次隔离打包演练的产物阶段用时 35.26 秒，八件资产与原候选逐件相同。远端上传和公开下载读回在实际发布时单独记录。

全量发现、lint/type 检查、Swift helper 和 Windows 原生矩阵可按需执行，见[检查分层](ci-scopes.md)。未执行的原生长回归按未运行记录。包解包 self-check 证明的是包字节，实机和真实凭据 UI 由独立验收覆盖。

## 发布顺序与恢复

发布重新构建并逐字节比对 bundle，校验预发结果与 main 祖先关系；读取所有渠道和既有 tag／资产，原子创建不可变 tag，创建或续传 draft，核验服务端摘要／大小，公开 Release，并实际下载全部计划资产比对。之后用每个 ref 自己的前像 lease **原子提升所有选中渠道**，读回再提升共用 installer latest。未选平台的冻结渠道也须保持不变，避免部分交付把新的 installer latest 降回旧版。

源码须包含先前运行包记录的 Source-Commit。历史 Mac 包没有该字段时仍保留原渠道祖先，并以当前 Windows 共享源码为前进下界。

任意失败保留整个输出目录和 progress.json，重试相同 bundle；既有且一致的资产只核验，缺少的 draft 附件续传。丢失回复后先读实际状态，不删除、覆盖冲突 tag／资产，不回退其他发布。GitHub Release 与 Git refs 不是同一远端事务，中途公开资产而渠道未提升时据实报告并恢复同一 bundle。所有平台发布共用一个 concurrency 组。

Actions 重跑 publish job 复用同次 prepare artifact。重跑整个 prepare 若渠道已提升，须恢复原 bundle，不能用 auto 新版本冒充同次重试。执行工具也必须恢复 plan 固定的原提交。14 天后 artifact 可能过期，维护者应保留长期交付证据。进程强杀留下 release.lock 时先核实原进程已退出，再移除锁。

`published-and-verified` 证明 GitHub 发布、选中 Git 渠道、latest 和公开下载。生产 Server catalog/cache 由管理侧消费 plan.json 的 runtimes/manifests，沿既有受限 catalog 流程另行核验。真实设备、ARM64/x86_64 硬件、凭据 UI、Hook trust 及 worker/model 重载分别报告；没有实测不会阻止获授权的源码和包交付，也不能宣称设备验收通过。发布工具不引入 SSH 主机、部署路径、新密钥或 Server 管理入口。

## 需要重新安装的结构发布

`prepare --installation-mode reinstall --targets all --version auto` 用于已审核的目录结构切换。默认 `upgrade` 仍要求完全相同的文件集合和凭据组件摘要；不会因为新结构而放宽旧客户端的升级校验。重装模式必须同时准备 Mac arm64、Mac x86_64、Windows amd64、Windows arm64，保留现有三个渠道及其祖先，不另建信任渠道。

重装模式仍逐项核对协议、状态 schema、Python 最低版本、能力与 migrations。`plan.json` 冻结 `installation.mode`、每个原渠道的新增／删除路径、凭据前后路径和摘要。Mac 凭据 Swift 源必须逐字节相同；旧 Windows `scripts/brain_windows.py` 只允许已审核的 `unique` 导入变为完整包名，其余字节必须相同。任意其他凭据实现变化、来源字节不符、部分平台发布或重跑时切换模式都会拒绝。发布／核验从原始源码重建整个审计，不接受手改计划。此项审计**不修改设备上的 profile、凭据 pin 或队列绑定**。

工作流对应 `installation_mode: reinstall`；`publish` 仍默认关闭。Release 正文明确标注 `REINSTALLATION REQUIRED`。旧布局无法原地更新，旧客户端应保留原活动包并报告 review-required。新安装使用当前入口。结构重装应建立同一 Server／账号的新设备登记。旧身份、凭据项、未发送队列和安装回执保留在原位置或归档，不作为新身份接续，旧队列不自动重放。

实际操作、已确认记忆的恢复范围和事务续接见[官方重装与续接](../installation/reinstall.md)。发行方不能用手改 pin、删除配置或清空队列代替重装事务。

Mac 安装器按原生架构选择 Python 3.12.15 的固定归档，ARM 与 Intel 分别独立校验 SHA256。摘要来自 [上游 20261009 正式发布](https://github.com/astral-sh/python-build-standalone/releases/tag/20261009) 的 asset digest；已具备 Python 3.11+ 时复用现有解释器。Windows 继续使用已验证的本机 Python，不引入下载或系统 Python 替换。
