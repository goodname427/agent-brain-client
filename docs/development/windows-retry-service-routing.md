# Windows 并发重试与服务选择

> 当前维护；2026-11-04。源码修复不代表设备已升级，平台验证以实际运行结果为准。

## 并发重试的私有文件检查

Windows Client 0.6.2 的隔离验收中，常规断线、重启、补传与重复请求通过，同 ID 两线程重试时一个调用报 `Owned private Windows item required`。验收未保存完整线程栈，不能确定当时失败的具体文件，也不能推断生产 Server 幂等性失败。

已定位一个能产生同样错误的代码路径：私有文件校验直接使用 `Path.stat().st_nlink`。[CPython Windows stat 实现](https://github.com/python/cpython/blob/3.13/Modules/posixmodule.c) 的慢路径在共享冲突或访问拒绝时可退回目录项信息；目录信息不填链接数，结果为 0。合成 API 回归将路径 stat 固定为 0、完整句柄元数据固定为 1，旧实现拒绝，新实现通过。

Windows 3.11／3.13 原生回归随后复现了主要竞态：锁文件先创建、再设置最终所有者和 DACL，另一调用在中间窗口进行所有者检查而失败。失败校验位于原清理块外，也可能遗留打开的锁句柄。锁文件现通过 [CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew) 的安全属性在创建时就固定所有者和私有 DACL；打开已有锁时系统忽略该描述符，仍完整校验而不重写权限。失败时关闭句柄。[LockFileEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex) 支持 EOF 以外的范围，因此不再在加锁前写初始化字节，避免其他调用已持锁时发生写冲突。

修复从一个只请求属性与安全描述符读取权限的 Win32 句柄获取文件类型、链接数、所有者和 DACL。句柄共享读／写／删除以兼容现有锁和原子替换，并用 `OPEN_REPARSE_POINT` 拒绝跟随最终重解析点；祖先路径检查保留。共享标志没有向其他用户授予权限。完整元数据不可用、链接数为 0 或大于 1、类型错误、所有者错误和不受信主体仍拒绝；不修改已有 DACL，不读取文件正文。

此路径用于 Windows Client 私有缓存、outbox、锁和登记资料；macOS 检查不变。`test_windows_private_metadata` 验证不完整路径 stat、失败关闭和句柄释放；`test_windows_native.WindowsNative` 包含真实 Win32 DACL、跨进程租约及十轮双调用同 ID 重试。原生测试在非 Windows 系统跳过，不能把合成 API 测试当作实机通过。

实现位于已发布的共享存储模块 `brain_lock.py`；被设备固定的 `brain_windows.py` 凭据模块原始字节不变。新文件仍先调用原保护函数；只有其已知的私有项后置检查报错才用原有 fd 重新执行完整检查，DACL 设置失败继续拒绝。没有更改凭据源码 pin、发布兼容门或设备政策。

## synthetic 服务的边界

验收原文显示 Agent 读取过本地 Skill／原生记忆提示，并实际调用另一个插件的 `agent_brain_synthetic_sandbox.synthetic_health`。健康结果只说明那个 synthetic 服务当次可调用，不能证明 Client outbox、生产采集或召回。现有证据不能区分宿主旧会话工具暴露、插件状态传播或外部指引残留。

本仓库可控制的修复：正式内容服务要求使用当前连接的 approved-content 服务；旧记忆、测试措辞和同名工具不能授权切换到 synthetic；正式服务不可用时报告阻碍，不启用其他插件。隔离补传测试使用临时 Client 与虚构传输。旧五工具桥的 synthetic 描述明确标识测试范围；SSH 个人模式的安装／复用提示和配置注释改为个人服务，不再错误推荐 synthetic。

这些改动随相应 Client／安装器源码交付，不修改其他插件设置、宿主权限、原生记忆或受管 Skill。外部 synthetic 插件是否仍被宿主暴露、模型是否遵循新指引，需要新会话与设备验收。
