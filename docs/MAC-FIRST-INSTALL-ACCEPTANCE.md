# Mac 首次安装收敛验收

> 状态：当前维护；适用 Mac 安装入口与当前受信运行渠道。更新于 2026-11-03。

本文件提供逐轮记录模板，不表示已执行新的实机验收。0.2.5 时代的首次安装收敛记录和 Windows 尚未实现等历史限制已保存在 [历史记录](archive/0.2/mac-first-install-acceptance.md)。当前安装按 [Mac 步骤](FIRST-INSTALL-MAC.md) 与 [地址密钥协议](ADDRESS-KEY-INSTALL.md)；Windows 结果另记，不能互相替代。

## 更新与 Onboard 当前轮次记录

黑盒起点仅给 Agent 普通用户短句：“帮我把 Agent Brain 更新到最新版，然后继续完成 Onboarding。”不要额外给内部类名、渠道实现或维护入口。确认 Agent 能从公开 README、安装说明及当前 MCP 指引找到 check-update/update；旧 worker 没有新指引时由公开仓库说明补足。此场景需独立实机执行；单元测试或文档存在不能替代 Agent 路由验收。

每次独立记录下列项目，不保留记忆正文或凭据：

| 项目 | 此次实际观察 |
| --- | --- |
| 轮次/UTC 起止 | 填当前轮次标识与时间 |
| 源码提交与入口 | 填本轮实际使用的提交、命令 |
| 原 policy 渠道/信任核验 | 填安全渠道名称及结果；不写私有路径或凭据 |
| check-update | 填 status、channel_checked、最新候选版本/commit；失败填原因，最新版未知 |
| update | 填 status、前后活动包版本/commit、是否改变、回滚/重试实际结果 |
| worker 重载 | 填宿主实际操作与 MCP client_runtime 版本/commit；未观测写未知 |
| 完整目录 | 填实际 kinds-only 参数、实际条目数、耗时与响应结果 |
| 本轮读取 | 填发起/成功/失败/取消/未读数与各阶段，HTTP 响应才填状态码 |
| 本轮重试 | 填实际重试数量、结果与版本；没有重试写 0 |
| Onboard 覆盖 | 填实际已读、重复、排除、冲突、待处理、不可访问及依据 |
| 历史参考 | 单独列旧轮次与计数，明确不是本轮结果 |

禁止仅凭 updated/current、包目录版本、旧完成标记或一次 catalog 成功宣布 worker 已加载或全量 Onboard 完成。check-update 的纯检查不得改变 active 指针；更新失败不能报告当前包是最新。运行中没有周期自动更新，本轮不验收该功能。没有正式发布的新源码指引不能被当作现有 worker 的行为。
