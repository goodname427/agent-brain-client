# 0.8：保留记忆，重新连接设备

0.8 使用新目录结构。已有旧安装应执行明确的 `reinstall`，不对旧布局原地 update，也不把普通 install 当成覆盖安装。新设备没有旧安装时继续使用普通 install。

本流程会建立新的设备连接。**旧身份和未同步队列不会自动接续或发送**；旧队列可能含有尚未上传的内容，它们只保留在归档中，不能声称已迁移到 Server。原 profile、pin、系统凭据项和旧文件不清理。Server 中已经保存的记忆不删除，原生 Codex 持久记忆留在原位置，不扫描或补传历史。

1. 退出旧 Codex 和 CodeBuddy，让旧 MCP worker 与 Hook 结束。保留当前用户配置和锁文件，不要删除它们来跳过“运行中”检查。
2. 从正式官方 main checkout 执行对应入口，并使用**原 Server、原账号**。安装器会在本机解释归档与重新连接行为；确认已退出旧宿主后输入 `FRESH`。

   ```sh
   # Mac（Apple Silicon / Intel）
   sh bootstrap/macos/install.sh --action reinstall --server 'https://your-server.example'
   # CodeBuddy IDE / CLI
   sh bootstrap/macos/codebuddy.sh --action reinstall --server 'https://your-server.example'
   ```

   ```powershell
   # 原生 Windows（amd64 / arm64）
   & ./bootstrap/windows/install.ps1 -Action reinstall -Server 'https://your-server.example'
   # CodeBuddy IDE / CLI
   & ./bootstrap/windows/codebuddy.ps1 -Action reinstall -Server 'https://your-server.example'
   ```

3. 在本机隐藏输入中重新填写该 Server/账号的长期接入密钥。不要把密钥放进聊天、命令行、环境变量或文件。安装器从该 Server 正常取得授权与发行信任信息；不会复用旧设备身份、伪造 pin、创建访问密钥或修改 Server 授权。账号或授权范围与原记忆不符时停止，不创建替代设备掩盖问题。
4. 核对输出中的 `retained_archive`、`memory_accessibility` 和 `installation_stages`。旧业务状态保留在 `~/.config/agent-brain-retained/<实例标识>/`；Python 和官方源码 checkout 等准备环境仍保留供当前安装使用。不要删除归档；其中的旧队列不由新安装自动读取或重放。
5. `known-memory-readable` 表示新设备已读取原来已知的记忆；没有旧的受管记忆 ID 时，`memory-catalog-readable` 仅表示新设备可读取记忆目录，不是完整性或 Onboard 覆盖率声明。已核验的本地 Memory/Rule 投影与受管文件归属由各自所有者保留并恢复，原生记忆文件不变。记忆缺失、网络失败或账号不符时保留现场，并按同一命令恢复，不能将失败当成迁移成功。
6. 原有全局排除目录、项目隔离／共享规则、非项目目录选择及个人记忆 recall 开关会保留；旧设备批准摘要和身份不复制，新设备仍通过正常流程登记。未来项目采集沿用正常的全项目默认，并遵守保留的排除与隔离边界。逐项完成 Mapping、未来项目采集与宿主 Hook trust 检查；只在观察到 worker 版本与活动包不一致时正常重载 Agent Brain MCP。模型加载与全量记忆覆盖仍单独报告。

中断后重跑**同一个入口、同一个 Server**。归档移动和宿主入口修改有独立恢复记录；不会再次归档半成品或重新发送旧队列。运行中 worker、原生配置外部改动、陌生入口、损坏的回执或未完成原生映射事务会要求处理具体冲突；保留原始文件，不能通过删数据、改摘要或套用示例配置继续。

新安装只是重新连接客户端；Server 存储迁移及其备份由 Server 的正式发布流程负责。这里的自动核验只覆盖新设备能读取的已知记忆和保留的本地投影，不会擅自改变 Server 数据或权限。
