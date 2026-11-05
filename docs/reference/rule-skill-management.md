# Rule / Skill 启停与本机同步

> 当前维护；适用包含 `content_set_enabled` 的 Client / Server 源码，核对日期 2026-11-05。合入源码不代表运行包已发布、Server 已部署或两台设备已生效。

## 用户入口

在官方 Client checkout 中，沿原更新入口取得协调发布后的 Client 运行包，并确认 Server 也包含启停 API。使用已有 HTTPS 设备身份打开临时本机管理页：

```sh
sh tools/memory.sh content-manage
```

Windows 使用同一个管理页和业务实现：

```powershell
& ./tools/memory-windows.ps1 content-manage
```

命令复用已安装的 Python、原注册、policy、系统凭据和缓存；不重新配对、不下载依赖。浏览器未自动打开时使用终端显示的本机地址；`--no-browser` 仅打印地址。Ctrl-C 关闭管理程序。页面不加载外部资源，只监听随机端口的 `127.0.0.1`，API 同时校验 Host、Origin 和短期页面会话 token；长期设备凭据不进入页面。

- **刷新列表**：读取 Server 实时目录与记录，显示共享启停状态及本机映射版本。读取失败或仅有离线缓存时，不把旧状态呈现为实时状态。
- **启用 / 停用**：只改变该现有 Rule / Skill 的 `enabled`，按用户看到的 revision 提交 CAS。保存确认后，使用原事务机制刷新本机映射。
- **拉取到本机**：拉取当前授权范围的 Server 快照并刷新本机映射，不重放待发送写入。它也用于已保存但本机文件同步失败后的恢复，无需再次提交保存。
- **使用原请求重试**：用于结果未知或阻断的启停请求，保留原 request_id、CAS 与目标状态。CAS 冲突则先刷新列表，检查当前版本，明确勾选冲突处理后重新选择目标状态；旧冲突证据保留并关联，不删除。

管理页分别报告 Server 已保存、GitHub 备份、本机文件同步与模型加载状态。`local-confirmed` 且 `local_durable:true` 确认 Server 本地持久保存，可以刷新本机；GitHub 备份仍单独待确认。已保存但本机刷新失败不会被称为同步成功，也不要求用新 ID 重复保存。页面显示的“与本次 Server 版本一致”仅证明此次读取的 revision 与验证过的本地映射相符；另一个设备之后仍可能更新 Server。

## 启停写入合同

`content_set_enabled` 是独立写方法，使用已有设备和 Server 的 `put` 权限与 kind / ID 范围，不授予删除、admin、其他 owner 或路径访问。只接受已有 Rule / Skill，Memory、缺失记录、客户端提供的 record / actor / owner / root / path 均拒绝。请求示例为合成资料：

```json
{"request_id":"synthetic_activation_12345","method":"content_set_enabled","params":{"kind":"rule","id":"synthetic-rule","enabled":true,"expected_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}
```

写入在 Server 锁内核对 CAS，从现有完整记录复制并仅替换 enabled，再验证触发模式、启用 Skill 的 frontmatter 和其原有内容合同。复用原 journal、提交、备份回执与幂等重放；重复同一已确认请求不重复修改，已经是目标状态的操作不制造内容提交。普通 `content_put` 继续要求新建 `enabled:false`，已有记录保持 enabled；正文编辑不是启停授权。MCP 的独立启停工具只用于用户明确要求启停的具体项目。

工具契约升级为 3。正常安装 / 更新通过原有受信事务登记新工具；只读检查保留经验证的旧 whitelist，不静默增加工具。契约 2 的未完成计划和最早三工具计划仅按精确前像迁移，旧 applying 安装事务仍使用原恢复材料。固定运行包文件集合、trust anchor 和设备 grant 格式保持原合同。

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
