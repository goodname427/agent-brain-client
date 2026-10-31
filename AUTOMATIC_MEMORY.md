# 日常项目记忆

日常配置使用 `until-disabled` 生命周期：来源持续到关闭或撤销，模型请求在累计额度耗尽、价格无法核对或结果不确定时暂停。不会按日清零，也没有 15 分钟或 1 小时使用期限。来源和费用需一次明确授权；默认不会把已关闭的旧测试授权转成日常授权。

一次配置后，从 Client 源码目录使用：

```sh
sh tools/memory.sh status --project-root /absolute/project
```

它只显示本地状态、项目 ID 和私有笔记目录，不读取记忆或凭据。

## A：项目中的 Codex 原生对话

仅在所选项目根目录启动的、支持官方 Hooks 的宿主中使用。正常信任该项目 Hook 后，UserPromptSubmit/Stop 按同 turn 配对，SessionStart 召回已有派生记忆并同步获准队列。不读原生历史目录、数据库或 transcript_path。不支持这些事件、缺 turn 或缺结束文本时不会伪造采集；改用 B。

服务端根据已认证的 owner/设备和所选项目正式登记今后的会话；这些登记不是原生 startup attestation。Hook 定义已登记不证明宿主实际触发，软件状态始终区分两者。

## B：主动提交一份笔记

不需要 GUI 或原生 Hook。在 `status` 显示的私有 `notes_directory` 内写一份 UTF-8 `.txt`、`.md` 或 `.json` 文件，权限为 0600，然后执行：

```sh
sh tools/memory.sh record --project-root /absolute/project --input /private/configured-notes/today.txt
sh tools/memory.sh recall --project-root /absolute/project
```

普通文本作为一条真实用户笔记提交，不构造助手消息。JSON 可用 `{"text":"要记住的内容"}`，或明确提供 `user`/`assistant` 文本对；可附稳定 `id`。相同内容/ID 的重试保留原事件及请求，不能改写原操作。每条文本最多 8192 UTF-16 单元。Server 接收和提取是不同状态，空事实不会冒称提取成功。

`recall` 返回 `additionalContext`，可直接交给当前任意 Harness 会话；支持的 Codex 宿主会在 SessionStart 注入。只返回来源校验通过的派生内容，不返回聊天 L0；没有派生内容时仍可召回既有获准 Data Memory，不将二者冒称已自动转换。离线结果标为缓存、未获远端确认。

## 关闭、恢复与离线队列

```sh
sh tools/memory.sh server-status --project-root /absolute/project
sh tools/memory.sh stop --project-root /absolute/project
sh tools/memory.sh resume --project-root /absolute/project
sh tools/memory.sh flush --project-root /absolute/project
```

`stop` 先停止本地自动入口，再请求 Server 停止该来源；断网时明确报告远端停止尚未确认。已发出的模型请求不能撤销，原预留保留。`resume` 恢复同一配置，并只处理明确尚未发送模型的暂停任务；未知结果不会自动重试。`flush` 只重放本来源已排队的原请求，不给它们换 ID。`revoke` 撤销来源，随后需要新的明确来源授权才能配置恢复。

识别到的凭据模式和 `[LocalOnly]`、`[no-memory]`、`[private]` 标记会被排除。它不是通用保密分类器，应选择适合共享的项目或明确笔记；Server 还会执行其既有敏感内容拦截。

## 已授权设备间召回

主设备导出不含正文/凭据的私有项目连接文件：

```sh
sh tools/memory.sh project-link --project-root /absolute/project --output /private/project-link.json
```

把文件交给同 owner 已安装、已授权的另一设备，在该设备执行 `connect-project --project-root /absolute/local-project --input /private/project-link.json`。复用该设备原 profile 和凭据，只连接召回，不增加采集或来源控制权限。

## 一次配置入口

Server 的 `setup_daily_memory.py` 根据明确批准的来源和累计额度生成完整策略包。Client 使用：

```sh
sh tools/memory.sh activate-bundle --project-root /absolute/project --activation /private/bundle/activation.json --activation-sha256 REVIEWED_SHA256
```

它验证原 owner、profile 和目标 Server，导入策略、保留旧设置并安装本项目 Hook，无需用户手改会话或拼策略。软件更新后 `enable --project-root ...` 刷新代码摘要。软件包已更新、来源已启用、实际宿主事件已交付是三个不同状态。
