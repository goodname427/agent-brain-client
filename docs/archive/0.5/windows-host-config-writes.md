> 历史记录：0.5.7 宿主写入设计及当时的验证／发布审批语境；当前技术正文见 [稳定入口](../../WINDOWS-HOST-CONFIG-WRITES.md)，当前发布按 [发布流程](../../RELEASE-PIPELINE.md)。

# Windows 宿主配置写入与恢复

本补丁修复 0.5.6 安装和恢复把 Codex 配置目录误当成 Agent Brain 私有目录的问题。`codex_connect.atomic()` 调用 `private_directory(path.parent)`；Windows 私有目录要求当前用户所有权，并拒绝其他主体的有效 Allow ACE。这适用于密钥、私有缓存和恢复记录，但不适用于由宿主管理、可能由 Administrators 所有且包含沙箱访问规则的 `.codex`。此前安装与恢复共用这一私有写入函数，因而可能连续失败。现有日志不足以证明用户实机上的完整异常调用栈；根因是源码路径核验所得。

安装事务只对计划内的 `config.toml` 和已登记的旧 Agent Brain 指令清理使用宿主写入方法；注册、计划、身份、配对记录和缓存仍走原私有写入方法。TOML 渲染仍只改变已批准的 Agent Brain MCP 表，并解析比较其他设置。包、来源、能力、原锚、命令、身份、摘要与路径检查保留；现有密钥不进入配置文件。没有修改凭据后端源码、其摘要迁移规则、PATH 或全局主体白名单，也没有恢复弃用接入。

## Windows 写入选择

既有文件使用 `CreateFileW`，申请 `GENERIC_READ | GENERIC_WRITE`，`FILE_SHARE_READ`，`OPEN_EXISTING` 和 `FILE_FLAG_OPEN_REPARSE_POINT`。写入保持原文件对象，不请求 DELETE、WRITE_OWNER、WRITE_DAC 或 ACCESS_SYSTEM_SECURITY，不调用权限设置 API，不读取 SACL。因此原对象的 owner、DACL、SACL、继承状态和其他流无需复制或重建。未知 SACL 不会成为新的安装门禁。实际访问仍由现有系统权限决定：文件只读、被其他写入者占用或没有数据写入权时，在数据修改前拒绝。

这是保留原对象的受控数据写入，不是整文件原子替换。文件句柄存续期间，系统共享模式排除普通并发写入及删除／重命名，允许读取。读取者可能短暂观察到部分内容。安装协议后检在写入与截断均完成并 flush 后运行；操作结束关闭句柄。元数据管理者和具有特殊系统权限的进程不属于这一共享模式对数据 I/O 的排斥保证。

新文件先在相同宿主目录创建并写入、flush，再用不覆盖目标的重命名发布；它继承宿主安全属性，不使用 Agent Brain 私有文件 DACL。若目标在预检后出现，发布停止。已有父目录不接管、不收紧、不递归处理。符号链接、reparse point、硬链接、非普通文件和超界文件继续拒绝。

Mac 路径保留同目录暂存及原子发布，保存既有 POSIX owner/group/mode；这不构成对所有扩展 ACL 或硬件断电行为的实测保证。

## 日志与重试

所有宿主文件先预检；数据提交前，私有事务日志持久保存批准计划摘要、完整前像、后像摘要，以及原位文件的 device/inode。写入从文件头开始覆盖，先 flush 数据，再截断到目标长度并再次 flush。

恢复只接受原对象上能由该计划的前像、目标内容和前缀覆盖／最终截断生成的状态。目标内容从批准计划重新计算并校验摘要，不信任任意新日志内容。恢复也逐项重新核对当前状态；外部编辑、对象替换、未知损坏或改变的计划均保留现场并报告需要审查。

回滚覆盖数据前，日志另记当前已验证覆盖位置及长度。回滚再次中断时，正常入口只接受该回滚意图可产生的中间内容，继续恢复并重试原计划。旧日志不含原位字段时仍只接受完整原前像／后像；旧私有策略在尚未写入宿主文件就失败的事务可以沿正常恢复入口继续。原身份和恢复记录保留，不要求用户手改 ACL 或重建凭据。

flush 和写前日志提供进程中断恢复边界，不保证硬件断电不会产生任意扇区损坏。不能解释为已登记写入形状的数据不会自动覆盖；原前像和日志保留。实际断电未测。

## 本地验证与未测范围

当前代码在隔离 HOME/CODEX_HOME 下完成 462 项发现：443 通过、19 平台跳过、零失败／错误。结果按同一源码指纹记录；恢复运行的辅助脚本曾漏加仓库根目录导致导入错误，修正后所有实际测试项均已完成。跳过项是 Windows 原生 API 和历史 Windows Python 环境；不是删除或放宽断言。localhost HTTP/TLS 测试在正式允许的本地测试运行权限下执行。

新增测试覆盖宿主与私有写入分离、已有／新文件、安装／配置替换、失败回滚、正常重试、部分写入、截断前中断、回滚再次中断、外部编辑、原对象替换以及 Win32 请求权限。原生 Windows 合成测试还准备了普通／Administrators owner、合成沙箱 SID、继承 DACL、共享句柄排斥、失败后检和普通入口恢复。生产路径不需要审计权限；仅用于观测 SACL 的原生测试在没有该观测权限时允许明确跳过。

适用 Ruff、现有三组 mypy、全部 Python 语法和公开材料扫描通过。Windows CPython 3.13/3.11 原生执行、ARM64 实机、用户桌面与真实 Codex 重载、真实断电均未测，不声称用户机器已安装成功。没有改真实 Codex 配置或 ACL。外部 CI 只增加测试检查配置，未触发。

## 另获发布审批后的具体动作

1. 审批明确最终提交、前向版本号、目标仓库／分支、安装器及可选同步运行包、tag／资产和渠道目标；本次本地提交不批准这些操作。
2. 获批后才推送审核分支、创建 PR 并运行已有 Mac 与 Windows 3.11/3.13 CI；检查原生 owner／DACL／共享句柄及恢复测试结果，明确条件跳过。用户实机验收由用户自行执行。
3. 审核通过后按另批范围合入。由精确提交创建新的安装器源码归档和 SHA256SUMS，不改写既有 0.5.6 tag、资产、已固定 bootstrap 或旧恢复材料。
4. 如批准同步运行包，使用 `tools/build_client.py --mode personal-content-https --platform win32-amd64 --version <批准版本> --output <新候选目录>`，ARM64 按批准目标另建；核对现有文件／能力范围及 manifest，生成资产与摘要。不得用本地候选绕过既有发行策略。
5. 仅按批准范围发布 tag／资产和渠道指针，保持原信任锚及来源范围，下载回验字节和摘要。无需部署 Server。普通安装入口复用原密钥、设备身份及恢复日志，不提示手改 ACL/PATH/摘要。

参考微软官方语义：[CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)、[文件安全与访问权](https://learn.microsoft.com/en-us/windows/win32/fileio/file-security-and-access-rights)、[GetFileSecurityW 的 SACL 权限要求](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-getfilesecurityw)。
