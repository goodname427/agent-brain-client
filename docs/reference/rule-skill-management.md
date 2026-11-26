# Rule / Skill 启停、归档与本机同步

> 当前维护；适用提供 `content_put(operation=set_enabled)` 的 Client 和支持 `content_set_enabled` 的 Server。

## 用户入口

在官方 Client checkout 中，沿原更新入口取得协调发布后的 Client 运行包，并确认 Server 也包含启停 API。使用已有 HTTPS 设备身份打开临时本机管理页：

```sh
sh bin/memory.sh content-manage
```

Windows 使用同一个管理页和业务实现：

```powershell
& ./bin/memory.ps1 content-manage
```

命令复用已安装的 Python、原注册、policy、系统凭据和缓存；不重新配对、不下载依赖。浏览器未自动打开时使用终端显示的本机地址；`--no-browser` 仅打印地址。Ctrl-C 关闭管理程序。页面不加载外部资源，只监听随机端口的 `127.0.0.1`，API 同时校验 Host、Origin 和短期页面会话 token；长期设备凭据不进入页面。

- **刷新列表**：读取 Server 实时目录与记录，显示共享启停状态及本机映射版本。读取失败或仅有离线缓存时，不把旧状态呈现为实时状态。
- **启用 / 停用**：只改变该现有 Rule / Skill 的 `enabled`，按用户看到的 revision 提交 CAS。保存确认后，使用原事务机制刷新本机映射。
- **拉取到本机**：拉取当前授权范围的 Server 快照并刷新本机映射，不重放待发送写入。它也用于已保存但本机文件同步失败后的恢复，无需再次提交保存。
- **使用原请求重试**：用于结果未知或阻断的启停请求，保留原 request_id、CAS 与目标状态。CAS 冲突则先刷新列表，检查当前版本，明确勾选冲突处理后重新选择目标状态；旧冲突证据保留并关联，不删除。

管理页分别报告 Server 已保存、GitHub 备份、本机文件同步与模型加载状态。`local-confirmed` 且 `local_durable:true` 确认 Server 本地持久保存，可以刷新本机；GitHub 备份仍单独待确认。已保存但本机刷新失败不会被称为同步成功，也不要求用新 ID 重复保存。页面显示的“与本次 Server 版本一致”仅证明此次读取的 revision 与验证过的本地映射相符；另一个设备之后仍可能更新 Server。

## 启停写入合同

在 Codex 和 CodeBuddy 中，用户明确要求启用或停用某个现有 Rule／Skill 时，用 `content_read` 读取其活动版本，再调用 `content_put`。以下是合成参数：

```json
{"operation":"set_enabled","kind":"rule","id":"synthetic-rule","enabled":true,"expected_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","request_id":"synthetic_activation_12345"}
```

Worker 将启停分流到 Server 的 `content_set_enabled`。它使用设备原有 `put` 权限、kind／ID 和 owner 范围，在 CAS 锁内只改 `enabled`。启停参数只接受已有 Rule／Skill、布尔 `enabled` 和当前修订号。普通正文编辑省略 `operation`、提交 `record`，新建 Rule／Skill 时用 `enabled:false`，编辑时保留当前值。先核对 Server 保存回执，再检查本机映射。

结果未知时，用同一请求 ID 和原参数重试。旧队列中的 `content_set_enabled` 同样通过 `content_put(operation=set_enabled)` 重试，保留全部原始参数和本地冲突关联。使用 `content_request_id` 时，`method=content_set_enabled` 仍生成启停专用的原有请求 ID。CAS 冲突需重读当前版本，再显式决定是否用新的请求 ID 提交。重复同一已确认请求复用保存回执，已处于目标状态的操作不产生新内容提交。

正常安装 / 更新通过原有受信事务登记工具，已登记包的只读检查使用该包的工具契约。既有安装事务继续使用原恢复材料。

## 正式 MCP 的可恢复归档

Client 0.8.31 候选的工具契约 4 适用于 Mac 和 Windows 的共用 HTTPS Worker。需要配套 Server API。设备仍使用原 read/put 授权、种类和 ID 范围。只为用户明确指定的 Rule 或 Skill 发起操作。

1. 用 `content_read` 传入确切的 `kind`、`id` 和 `include_archived: true`。活动条目返回 `archived: false` 和完整记录。已归档条目返回 `archived: true`、`record: null` 和可用于恢复的修订号。未创建的 ID 返回 `missing`。显式读回要求 Server 实时响应。
2. 用 `content_archive` 或 `content_restore` 提交同一目标、读回的 `expected_revision` 和稳定的 `request_id`。结果未知时用原 ID 和参数重试。遇到冲突则重新显式读回，按用户明确选择发起新的 CAS 请求。原冲突在队列中可查，后续已确认的写入可以正常刷新映射。普通编辑和启停使用省略 `include_archived` 时的修订号。
3. 核对 Server 回执及本机映射状态。归档后条目退出活动目录和受管映射。恢复保留原 ID、正文和资源，并保持停用。需要启用时，另用 `content_put(operation=set_enabled)`。

旧 Server 拒绝生命周期操作，普通读取、目录、编辑和启停仍可使用。归档和恢复是 MCP 工具，临时本机管理页继续提供启停和拉取。

开发复验使用 [隔离跨仓检查](../../dev/integration/content_lifecycle.py)，传入对应 Server PR 候选及 0.3.8 的独立干净 checkout。它构建两平台的未发布候选清单，在执行机器上通过 stdio 和本机 HTTPS 检查 Worker，使用合成设备与内容。

## 本机与跨端语义

Rule 正文缓存保留；启用时按触发模式进入批准的全局 AGENTS 受管区块，停用后从该区块移除。Skill 启用时映射到设备的 `.agents/skills/<id>/`，停用仅移除该缓存所有的文件。不会执行 Skill 脚本、导入模块或自动安装依赖。

保留原有自定义 AGENTS 内容和无关本地 Skill。同名非受管文件、受管文件外部改动、缺少已批准依赖、Windows 文件占用或权限失败会阻断本机刷新并保留原文件；Server 回执独立保留。修复具体原因后点击拉取，不放宽权限、依赖或所有权检查。

其他设备通过正常 MCP 启动或本机管理页的显式拉取获取共享 enabled 变化。目录 / 内容读取不刷新映射，也没有承诺后台实时推送。文件同步不代表运行中的模型已重载指令或 Skill；模型加载状态保持 unknown，按实际宿主正常开启新会话或刷新上下文后另行验收。

## 发布后的独立设备验收

先协调部署含此 API 的 Server，再由原受信渠道更新两端 Client，并检查活动包、运行 worker 与宿主工具登记的版本 / commit；不更换身份或清队列。用户分别选择可恢复的测试 Rule / Skill，保存原 enabled 和 revision：

1. 在设备 A 明确启用，核对 Server 回执、写后 read、AGENTS 受管区块或 Skill 文件与本机同步状态。
2. 在设备 B 刷新列表，核对共享状态变化但读取不改变本机文件；点击拉取，再检查文件。
3. 在 A 停用，并在 B 拉取，核对对应本机产物退出映射且自定义内容仍在。
4. 对失败明确区分未保存、结果未知与已保存但本机同步失败；未知结果使用原请求重试，本机失败只重试拉取。最后按原状态恢复，并在新会话验证模型实际采用情况。

本轮开发只使用临时隔离资料；不能据此声称用户 Mac 与 Windows 实机已生效。离线单元回归在 `tests/test_content_activation.py`，跨仓真实 HTTP / 两个隔离设备的验收由 Server 的 `tests/integration/content_activation.py --client-root <明确的独立 Client checkout>` 执行。
