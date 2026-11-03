# 按变更范围运行 Client 检查

> 当前维护；Client 0.6 开发。核对日期：2026-11-03。

普通 PR 和 main push 都启动 [Client checks](../../.github/workflows/client-checks.yml)，由 [分类器](../../tools/ci_scope.py) 选择 job。没有工作流级 paths 过滤；文档标点改动不会申请 Mac 或 Windows runner。轻量检查包括全仓文档链接／锚点、文档检查器与 CI 分类／汇总回归、diff 格式。它不证明代码或平台已完整验证。

| 变更 | 轻量文档检查 | 源码检查／lint／类型 | Mac 完整标准库测试与 helper 自检 | Windows 原生合同（3.11、3.13） |
| --- | --- | --- | --- | --- |
| 根目录或 docs 下的 `.md`，含删除、同类改名 | 是 | 跳过 | 跳过 | 跳过 |
| 明确的 Mac 专用文件：三个 native Swift、helper 构建器、install-mac.sh、memory.sh | 是 | 是 | 是 | 跳过 |
| 明确的 Windows 专用文件：brain_windows.py、access_windows.py、三个 PowerShell 入口、test_windows_native.py | 是 | 是 | 跳过 | 是 |
| 公共代码、其他测试／fixture、依赖、构建／发布、工作流、固定 JSON、新文件或未知路径 | 是 | 是 | 是 | 是 |
| 混合改动／跨平台改名 | 是 | 按范围并集 | 按范围并集 | 按范围并集 |
| 手动 Run workflow、merge queue、基准缺失／不可判定 | 是 | 是 | 是 | 是 |

平台专用路径是短小的精确白名单，新增文件默认走全量，确认边界和测试覆盖后才加入白名单。`tools/mac_first_install.py` 虽然带 mac 名称，实际也承接 Windows 安装，因此归公共代码；`windows_install_complete.py` 及 POSIX 故障注入 fixture 同样保守覆盖两端。依赖和 CI 配置不按目录名猜测。docs 下的连接 JSON 是合同，仍跑全量。Markdown 变成可执行文件、符号链接或子模块等特殊变更走全量。

## 基准和结果

分类 job 完整 fetch Git 历史，不使用 GitHub 路径过滤或有数量限制的 PR 文件 API。PR 比较事件 base 与 head 的 merge-base 到 head，覆盖整个 PR；main push 比较事件 before 到 after，覆盖多提交合并。强推前像不在祖先链、新分支零前像、找不到对象、空 diff 或解析失败均走全量。Git NUL 分隔保留空格／换行路径，禁用 rename 检测，把删除旧路径和新增新路径一起分类，防止代码改名成 Markdown 后漏测。文档 job 每次检查全仓链接，所以删除目标或改名留下断链会失败。

所有 checkout 固定 `github.sha`：PR 测试当次合成 merge，push／手动运行测试当次提交；不会在排队时跟随移动分支。分类范围针对 PR 自身，测试对象仍是合并后的代码。

`Client checks required` 始终运行，要求分类和文档成功、每个选中的 job 实际成功、未选中的 job 明确跳过。失败、取消或缺失分类结果都不能让汇总变绿。摘要里的 false／skipped 表示未验证该范围。`Client full validation` 只有两端及源码检查全部成功才成功，局部检查时明确跳过。

2026-11-03 的仓库 API 核对：main `protected:false`，required status contexts 为空，rulesets 为空；本变更不增加审批或调整保护。需要将来配置必需 check 时使用稳定的 `Client checks required`，不要要求条件运行的单个平台 job 或 `Client full validation`，也不要给整个工作流增加 paths-ignore，否则可能永久 pending。[GitHub 的路径过滤说明](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore)解释了这种 pending 行为。

## 完整验证、发布与封板

在 Actions 的 **Client checks → Run workflow** 选择目标 ref，可强制所有检查；不需要额外审批输入。手动运行固定当次 ref 的 SHA。正式发布／阶段封板必须记录对应最终源码树的完整验证证据；文档合入后的绿色局部 CI 不可冒充封板。若最终提交只有文档变化，先对该提交运行全量 Client checks，再执行独立授权的发布／封板。

[发布工具](../../tools/release_client.py)继续核对精确源码树与 PR 合成合并语义，并读取同一次 run attempt 的实际 jobs：分类、文档、源码、Mac、Windows 两版本、汇总和完整标记必须全部 success；任何 skipped、cancelled、failure、缺失或重复证据均拒绝。它允许 main push、main 手动全量以及满足既有树条件的 PR，不凭祖先提交的绿色状态替代当前树。更早的无完整标记工作流也不能作为新的发布证据。发布准备自身的完整离线测试和现有不可变资产／渠道校验继续执行。

合成 Git 历史、各类路径／改名／删除／混合、失败汇总与发布证据回归见 [test_ci_scope.py](../../tests/test_ci_scope.py) 和 [test_release_pipeline.py](../../tests/test_release_pipeline.py)。平台原生检查仍以 GitHub 对应 runner 的实际结果为准；合成测试不证明真实桌面、设备或生产验收。
