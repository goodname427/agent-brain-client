# 固定发布入口

> 状态：当前维护。适用：Mac / Windows 共用发布工具；执行发布仍需该动作授权。文档核对日期：2026-11-04。

`dev/release/client.py` 复用现有构建器、HTTPS 文件清单、公开扫描、隔离测试与离线 self-check。正常发布默认 `targets:all`，同时构建 Mac arm64/x86_64 和 Windows AMD64/ARM64，使用同一个组件版本、一个安装器源码归档及同一套发布／读回流程。Swift 与 Win32 凭据文件集合分别校验，各自凭据源码 pin 不变；构建不授予新的平台权限。

## 渠道与不可变版本

Windows 保留 `refs/heads/releases/personal-https-windows-stable`。Mac 保留生产登记的 `refs/heads/releases/personal-https-stable`，以及已有设备实际固定的 `refs/heads/releases/personal-https-candidate-0.4.0-install-fix-ce9f72b`；不根据名字把设备切换到别的 ref。Mac 两个既有 ref 消费同一 Mac 运行树，新提交将两个旧 tip 分别保留为直接父节点。既有锚点、政策、权限、凭据 helper 和服务器配置不变。

`auto` 从所有冻结渠道的最高版本取下一补丁。正常发布包含两个平台组；`targets:macos` 或 `windows` 仅用于补齐部分交付，仍同时构建和校验两端 manifest。显式版本必须高于所有要提升的渠道版本，且不得低于任何未选平台的冻结版本；补齐旧平台不能降低共用 installer latest。安装器版本已存在时只能复用完全相同的源码、tag 和资产，不得以新 tag 为同一安装器版本换源码。

运行包 tag 分别为 `client-VERSION-windows` 和 `client-VERSION-macos`；每个平台组各有两架构归档及 SHA256SUMS。新共用安装器使用 `installer-VERSION-setup`。补齐已有 Windows 版本时复用原 `installer-VERSION-windows-setup` 及其原始资产，不重新上传 Windows 运行包或已公开的安装器。旧 tag、Release、附件和 schema-1 Windows bundle 保持可核验及可恢复。

## 正常操作

功能源码合入并通过完整 **Client checks** 后，在 main 的 **Client release** 工作流填精确源码 SHA，版本默认 `auto`、targets 默认 `all`。默认只准备；选择 `publish` 才进入独立写权限 job。局部绿色检查不能证明可发布，必要时先对最终 main 手动运行完整检查。准备 job 不持有发布凭据；发布 job 使用既有 GitHub Actions 临时身份，只申请 contents:write/actions:read，不创建 token 或环境。

工作流分别固定产品源码和执行工具的提交。工具来自本次工作流的 main SHA；产品源码可为已合入、已通过完整 CI 的历史精确 SHA，以便补齐已发布版本的另一平台。两份 checkout 均要求官方 origin、干净工作区和 Git blob 原始字节；发布前分别核验源码与执行工具的完整 CI、main 祖先关系。它不会从新工具目录把额外文件带入旧产品包。

本地等价入口（两个路径均为干净的独立官方 checkout，Python 3.11+；同一提交时可使用同一路径）：

```sh
python3 -B "$RELEASE_TOOLS/dev/release/client.py" prepare --release-notes-file "$RELEASE_NOTES_FILE" --source-root "$SOURCE_CHECKOUT" --source "$SOURCE_SHA" --targets all --output "$RELEASE_OUTPUT"
python3 -B "$RELEASE_TOOLS/dev/release/client.py" publish --source-root "$SOURCE_CHECKOUT" --output "$RELEASE_OUTPUT" --allow-publish
python3 -B "$RELEASE_TOOLS/dev/release/client.py" verify --source-root "$SOURCE_CHECKOUT" --output "$RELEASE_OUTPUT"
```

prepare 不访问网络、凭据或设备，不推送、不安装；先正常 fetch 渠道快照。它冻结源码树、执行工具、各渠道前像、版本、平台、安装器 tag 和确定性资产，运行完整隔离测试、源码／公开扫描及 diff 检查。审阅输出的 plan.json、release-notes.md 和检查日志后再发布。本地 publish 仅消费已有授权会话的 GH_TOKEN 和原 Git 推送身份；没有授权时停止，不另建凭据。

## 每版更新日志

启动 `Client release` 时必须填写 `release_notes`；本地 `prepare` / `prepare-local` 使用 `--release-notes-file` 指向 UTF-8 Markdown 文件。内容只包含可公开的新增、修复、影响与升级／重载提示，不填写凭据、个人资料或私有日志。空白输入在构建前拒绝，正文沿用公开内容扫描。

prepare 将这份日志放在 Release 正文顶部，保留后面的源码、运行包、manifest 摘要和验收边界，并在 plan 中固定完整正文摘要。同一 bundle 的重试不得换日志；publish 不再接收另一份文本，自动将准备好的正文用于本轮新建的安装器和平台 Release。补齐平台时已有安装器 Release 不改写。检查 `release-notes.md` 后再发布；流程不会自动改写历史版本，也不会仅因日志功能自动增加产品版本。

## Actions 额度耗尽时

实际工作流只有 `client-checks.yml` 与 `client-release.yml`。检查工作流的文档、源码扫描、固定版本 lint/type check、Mac 全量测试／Swift helper self-check、Windows 原生矩阵在 [client_checks.py](../../dev/checks/client.py) 有完整本地对应命令，回归测试逐项核对工作流中的命令未被遗漏；原 Actions 配置保持不变。发布工作流的 prepare、publish、verify 仍使用同一个 [release_client.py](../../dev/release/client.py)。分类及汇总是 Actions 调度逻辑，本地正式发布总是要求全部检查，不把文档或单平台绿色当全量。GitHub 仓库、refs、Release 和既有授权仍须可访问；这不是全站离线模式。

在独立、干净、官方 origin 的已提交 Client checkout 中操作。用隔离 Python 环境安装 `ruff==0.14.0 mypy==1.18.2`，Mac 准备使用 Python 3.12 和现有 Swift 工具链。输出保存在 Git 外私有目录；日志可能含本机路径，不上传为公开附件。所有检查子进程隔离 HOME、USERPROFILE、CODEX_HOME，移除发布 token，不安装／升级真实设备、不访问 Keychain 密钥或执行模型调用。

```sh
python3.12 -B dev/release/client.py prepare-local \
  --source "$SOURCE_SHA" --targets all --output "$RELEASE_OUTPUT" \
  --release-notes-file "$RELEASE_NOTES_FILE"
```

`prepare-local` 要求产品与执行工具为同一 checkout；重新读取官方 main 并验证源码已合入，刷新三个既有渠道前像，执行 documentation、source、macos 全部对应检查，然后使用原构建器生成确定性双平台／四架构运行包及共用安装器。检查前后核对精确提交、干净状态和 Git blob 字节；任何检查失败不写成功回执，也不开始构建。不提供跳过检查开关。原 `prepare` 继续提供不联网打包路径，默认 publish 仍核验 Actions；不能因 bundle 可构建就声称可发布。

输出 `local-checks/{documentation,source,macos}/checks.json` 及同目录逐命令日志；`local-preparation.json` 明确标记 `github_actions: not-used`、Windows 两版本 `not-run`。本机 Mac 结果不能替代 Windows 检查。需在**独立的原生 Windows checkout** 切到相同源码树，使用现有 Git、PowerShell 7（`pwsh`）与 Python 3.11、3.13 分别执行（PowerShell）：

```powershell
py -3.11 -B dev/checks/client.py windows --record --output C:\private\checks-3.11
py -3.13 -B dev/checks/client.py windows --record --output C:\private\checks-3.13
```

Windows 命令复用原生凭据／DACL、生命周期、安装／离线恢复、路径、MCP、队列、写失败、召回／重载回归与两个 PowerShell 脚本解析检查。仅在 Windows 对应 Python 版本生成回执；不能在 Mac 用被跳过的 Windows 测试补造证据。既有测试对当前 token 无权核验的个别 Windows 安全描述符仍可能跳过，应检查原始日志；不通过提升 token 权限消除跳过。架构包是源码归档，四架构构建和 self-check 不代表四架构硬件、真实凭据 UI 或用户验收。

将两个 Windows 证据目录（含全部日志）保留到操作者的私有证据目录，在 Mac 显式选择本地证据发布：

```sh
python3.12 -B dev/release/client.py publish --output "$RELEASE_OUTPUT" --allow-publish \
  --local-check "$RELEASE_OUTPUT/local-checks/documentation/checks.json" \
  --local-check "$RELEASE_OUTPUT/local-checks/source/checks.json" \
  --local-check "$RELEASE_OUTPUT/local-checks/macos/checks.json" \
  --local-check "$WINDOWS_311/checks.json" --local-check "$WINDOWS_313/checks.json"
python3.12 -B dev/release/client.py verify --output "$RELEASE_OUTPUT"
```

发布前逐项核验源码树与执行工具树的完整五份回执、原生 OS／Python 版本、当前本地命令清单、每条命令退出码和日志摘要；不完整、重复、错树、日志被改或检查命令过期均在任何远端写入前拒绝。没有 Windows 主机时可以完成 Mac 检查和包准备，正式本地发布保持阻塞，不能宣称全量通过。单独开发验证可使用 `client_checks.py <scope> --output <新目录>`；只有 `--record` 对干净已提交源码记录可供发布的证据。

本地证据是操作者保管的回执和日志，**没有签名，也不是 GitHub CI 证明**；仅应从实际运行命令的受信操作者接收。`ci.json` 明确保存本地模式和两份树的原始证据，不写 GitHub 成功状态，不修改分支保护、审批或仓库权限。正常 Actions 路径的全部 jobs 门禁保持。两种证据路径共用后续不可变 tag／资产核对、原包下载验证、渠道前像 lease 原子提升及 latest 读回。

### 本地 Mac 与官方 Windows CI 混合验证

GitHub 的 Mac runner 排队或不可用，但同一源码树的官方 Windows 作业已经完成时，可以显式选择 `--hybrid-checks`。它要求产品源码树与发布执行工具树各自具备三份完整本地 `documentation`、`source`、`macos` 回执及原日志；两份树相同时复用同一证据。Windows 必须来自官方 `client-checks.yml` 的同一次 run attempt，`changes`、`documentation`、`source-checks`、`windows-contracts (3.11)`、`windows-contracts (3.13)` 均须实际完成且成功。发布时在线重新读取 GitHub API，核对官方仓库、精确源码树、main push/manual main 或 main 已为 head 祖先的 PR、完整分页及每项唯一 job；缺失、跳过、失败、重复、其他树或 fork 均拒绝。不能把不同 attempt 的两个 Windows 作业拼成成功。

```sh
python3.12 -B dev/release/client.py publish --output "$RELEASE_OUTPUT" --allow-publish \
  --hybrid-checks \
  --local-check "$RELEASE_OUTPUT/local-checks/documentation/checks.json" \
  --local-check "$RELEASE_OUTPUT/local-checks/source/checks.json" \
  --local-check "$RELEASE_OUTPUT/local-checks/macos/checks.json"
```

`ci.json` 记录 `mode: hybrid`，分别保存真实本地回执、官方 Windows 的 run/attempt/job ID、实际 workflow 状态与结论。整体 workflow 可以仍在排队或因 Mac 作业未完成而失败；这里仅确认列出的官方 Windows 等作业通过，Mac 由完整本地检查覆盖，不宣称 GitHub 全量成功。三个本地 scope 或两个 Windows 版本任一缺证据均在推 tag、上传附件或提升渠道之前停止。不生成或改写 Windows 本地回执，不写 GitHub 成功状态，不修改 workflow、权限或分支保护。默认全 CI 和五份纯本地回执路径保留原门禁，只有明确传入此开关才使用混合验证。版本、包字节、信任锚、凭据 pin、main 祖先核对、原子渠道 lease 与资产读回仍沿用原流程。

本地检查输出禁止覆盖。失败保留日志，在新输出目录重跑；已有正式／部分发布的原 bundle 不删除，恢复时用原 `publish` 加完整证据继续，不重新 `prepare-local` 替换既有身份。已有 Actions 成功记录也可继续用原 publish。跨版本补齐时默认仍用分离源码／工具的 Actions 路径；本地新入口专用于同 checkout 的后续版本，不放宽旧版本 pin。

## 发布顺序与恢复

发布重新构建并逐字节比对 bundle，校验两份完整 CI 与 main 祖先关系；读取所有渠道和既有 tag／资产，原子创建不可变 tag，创建或续传 draft，核验服务端摘要／大小，公开 Release，并实际下载全部计划资产比对。之后用每个 ref 自己的前像 lease **原子提升所有选中渠道**，读回再提升共用 installer latest。未选平台的冻结渠道也须保持不变，避免部分交付把新的 installer latest 降回旧版。

完整 CI 要求同一 attempt 的分类、文档、源码、Mac、Windows 两版本、汇总和完整验证标记 jobs 全部实际成功。允许复用相同源码树的成功 PR，但仅当其 main 基线已是 head 祖先；不能把含额外基线修改的合并树结果冒充独立 head。源码须包含先前运行包记录的 Source-Commit；历史 Mac 包没有该字段时仍保留原渠道祖先，并以当前 Windows 共享源码为前进下界。

任意失败保留整个输出目录和 progress.json，重试相同 bundle；既有且一致的资产只核验，缺少的 draft 附件续传。丢失回复后先读实际状态，不删除、覆盖冲突 tag／资产，不回退其他发布。GitHub Release 与 Git refs 不是同一远端事务，中途公开资产而渠道未提升时据实报告并恢复同一 bundle。所有平台发布共用一个 concurrency 组。

Actions 重跑 publish job 复用同次 prepare artifact。重跑整个 prepare 若渠道已提升，须恢复原 bundle，不能用 auto 新版本冒充同次重试。执行工具也必须恢复 plan 固定的原提交。14 天后 artifact 可能过期，维护者应保留长期交付证据。进程强杀留下 release.lock 时先核实原进程已退出，再移除锁。

`published-and-verified` 证明 GitHub 发布、选中 Git 渠道、latest 和公开下载。生产 Server catalog/cache 由管理侧消费 plan.json 的 runtimes/manifests，沿既有受限 catalog 流程另行核验。真实设备、ARM64/x86_64 硬件、凭据 UI、Hook trust 及 worker/model 重载分别报告；没有实测不会阻止获授权的源码和包交付，也不能宣称设备验收通过。发布工具不引入 SSH 主机、部署路径、新密钥或 Server 管理入口。

## 需要重新安装的结构发布

`prepare --installation-mode reinstall --targets all --version 0.8.0` 用于已审核的目录结构切换。默认 `upgrade` 仍要求完全相同的文件集合和凭据组件摘要；不会因为新结构而放宽旧客户端的升级校验。重装模式必须同时准备 Mac arm64、Mac x86_64、Windows amd64、Windows arm64，保留现有三个渠道及其祖先，不另建信任渠道。

重装模式仍逐项核对协议、状态 schema、Python 最低版本、能力与 migrations。`plan.json` 冻结 `installation.mode`、每个原渠道的新增／删除路径、凭据前后路径和摘要。Mac 凭据 Swift 源必须逐字节相同；旧 Windows `scripts/brain_windows.py` 只允许已审核的 `unique` 导入变为完整包名，其余字节必须相同。任意其他凭据实现变化、来源字节不符、部分平台发布或重跑时切换模式都会拒绝。发布／核验从原始源码重建整个审计，不接受手改计划。此项审计**不修改设备上的 profile、凭据 pin 或队列绑定**。

工作流对应 `installation_mode: reinstall`；`publish` 仍默认关闭。Release 正文明确标注 `REINSTALLATION REQUIRED`。旧布局无法原地更新，旧客户端应保留原活动包并报告 review-required。新安装使用当前入口。已有安装必须保留本地 Memory、Server 数据、身份、凭据、待发送队列和安装回执；不得删除配置目录、清空队列、重置设备身份或改写 pin 来获得安装成功。

旧 Windows profile 的摘要绑定旧凭据源；新版继续拒绝旧 pin。结构切换使用显式 `reinstall` action：在正常关闭旧 Codex/CodeBuddy 后，仅移除回执核验一致的 Agent Brain 入口，将旧 Client 状态移动到独立保留目录，再由用户通过隐藏输入重新连接同一 Server/账号。新 profile 有独立 instance_id，原系统凭据项与旧 pin 不改写、不清理；旧身份和旧队列不接续。已确认的本地记忆投影由 mapping 所有者验证并复制，原生持久记忆保持原位置；新连接读取已知原记忆进行核验。具体命令和恢复步骤见[0.8 重装](../installation/reinstall.md)。

Mac 安装器按原生架构选择 Python 3.12.15 的固定归档，ARM 与 Intel 分别独立校验 SHA256。摘要来自 [上游 20261009 正式发布](https://github.com/astral-sh/python-build-standalone/releases/tag/20261009) 的 asset digest；已具备 Python 3.11+ 时复用现有解释器。Windows 继续使用已验证的本机 Python，不引入下载或系统 Python 替换。
