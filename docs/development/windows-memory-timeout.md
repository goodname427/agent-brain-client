# Client 0.6：Windows 记忆搜索／读取超时排查

## 报告与证据边界

报告中的 Windows MCP worker 是 0.5.9，运行提交为
`db623a805e726e8f1cbc980254c56f3a45bb1e4e`。搜索与正文读取均由宿主在
120 秒后报超时；目录曾成功返回。这只能证明目录调用成功，不能证明正文
可读、正文不存在或搜索没有结果。本次不使用真实记忆正文或测试暗号。

独立开发基线是 main `3dc6fa027da46369c86e4fd59ad0cd4251de8dd9`。
该基线的 `content_mcp.py` 与 `brain_http.py` 相对报告运行提交没有变化。
没有 Windows 用户设备控制授权，未访问该设备、生产凭据或 Server 工作区。
以下均为合成测试证据，不代表真实 Windows 问题已完成设备验收。

## 已证实的 Client 缺陷

MCP 使用一个 adapter executor，接收线程可以接受取消，读请求的 90 秒
截止时间包含排队时间。但旧实现的定时器只标记 RequestControl；排队请求
只有被 executor 取出后才检查并响应。活动持久写入不被中途取消，因此其网络
等待、锁等待或完整刷新尚未结束时，后面的搜索／读取即使已经过期，也不能
给宿主回复。该缺陷不要求 Server 或网络发生故障就能复现。

`test_mcp_queue_deadlines` 用保持阻塞的合成写入、100ms 排队截止时间和
700ms 响应上界复现。未修改的基线中三个用例全部失败：排队读截止时间、
排队取消及排队写截止时间都要等待活动写入释放。测试通过清理事件释放写入，
不会真的等待宿主的 120 秒。

修复将有界队列改为由同一锁保护的 deque / Condition。取消回调原子地移除
尚未开始的条目，释放 pending 容量并立即回复；executor 已取出的条目仍由
原来的请求控制处理。取消和取出竞争只有一方能处理条目。活动持久写入仍完成
其原事务和回执，不拆出后台 adapter 操作，也不增加并行写入。

JSON-RPC 取消／超时错误保留原 code 和 message，增加 `error.data.stage`：
`queue` 表示未进入 executor，`execution` 表示已被 executor 取出；后者本身
不能区分 DNS、连接、凭据助手、Server 处理或响应传输。既有 HTTP 错误的
transport / http 阶段仍按原合同返回，不把超时伪装为空结果或正文缺失。

## Windows CI 证实的传输取消缺陷

PR 首次提交 `67da2f3` 的 Windows 3.11 / 3.13 CI 中，新队列回归通过，
但既有 HTTP 和 TLS 活动读取取消失败：合成 Server 的 3 秒阻塞结束后才恢复，
超出测试的取消上界。Mac 上相同套件通过，因此不能用 Mac 结果替代 Windows。

旧读取依赖另一线程 shutdown / close socket 唤醒阻塞的原生 receive。Windows
CI 已证明这个唤醒不可靠。新增回归模拟 socket abort 不唤醒 receive：旧传输
仍等待 socket 超时，新实现由 executor 自己观察取消，不依赖该唤醒。

有 RequestControl 的 HTTP 响应改用非阻塞 socket 接收与最多 50ms 的 readiness
轮询。每次等待检查取消及截止时间，HTTP 解析和 TLS 验证仍使用标准库。
原生 makefile lease 保持 descriptor 生命周期，避免 urllib 在收到 headers 后
关闭连接对象时提前关闭正文读取。响应结束或失败仍释放 lease / socket；
没有额外读取线程或迟到的 adapter 操作。不在请求控制内的写入传输保持原语义。
该修复覆盖已复现的 headers / body 等待；不以它推断用户设备的 DNS、连接、
TLS 握手或 Server 根因。

## 回归与剩余不确定性

- 阻塞写入期间，搜索、读取、目录及排队写入均能独立返回截止时间错误。
- 反复取消并复用 JSON-RPC ID 不耗尽队列、不重复响应；被取消请求不执行。
- 活动写入保留回执，完成后下一读取继续；输出断开后不启动新 adapter 操作。
- 合成 HTTP Server 收到搜索后延迟响应、读取正文慢速传输时，活动请求有界终止，
  下一读取继续；合成 DNS 阻塞时，请求尚未发送且取消后不会迟发。
- TLS 检查仍开启，取消只关闭本请求 socket／助手进程；不放宽信任或设备授权。
- Windows CI 的 Python 3.11 / 3.13 明确运行新队列回归及 HTTPRequests
  传输测试。测试凭据为合成来源；这不替代 Windows 原生凭据套件或用户设备验收。

隔离 HOME / CODEX_HOME（Windows 另隔离 USERPROFILE），使用 Python 3.11+
及包含 `scripts:adapters/codex:installer:tools:tests` 的 PYTHONPATH，运行：

```sh
python -B -m unittest test_mcp_queue_deadlines test_content_read_requests test_memory_query test_remote_mcp
```

TLS 用例通过可用的 OpenSSL 创建临时合成证书；缺少该可执行文件时明确跳过。
没有生产的请求阶段日志，无法证明报告中的请求是否在排队、是否到达 Server，
也无法排除远程链路或 Server 延迟。当前交付修复的是已复现的队列响应缺陷，
不是对所有真实超时原因的推定。源码、CI、发布及真实设备验收须分别报告；
本变更不发布运行包、不创建 tag、不改变渠道或安装指针。
