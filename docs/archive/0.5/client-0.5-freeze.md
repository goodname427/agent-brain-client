# Agent Brain Client 0.5 阶段封板

> 不可变归档；适用 Client 0.5 阶段、安装器与 Windows 运行包 0.5.9。记录日期：2026-11-03。用户已明确授权 Client 完成后先封板，不等待 Server。

本次固定 **Client** 已交付源码、文档与组件映射。0.5 是阶段编号；安装器／Windows 包版本为 0.5.9，Server 软件版本另行维护。本记录不宣布 Server 封板，也不把一次设备安装验收表述为全部场景验收通过。机器可读基线与资产摘要见 [FREEZE-0.5.json](../../../FREEZE-0.5.json)。

## 源码与不可变标记

- 封板前 main：`b2e5b9abcbd2ff6dc5cdac827301cbc62f9b5de4`；包含文档 PR [#79](https://github.com/goodname427/agent-brain-client/pull/79)（`c60824bc4009aeb97b4f49a6cf7ddcd700de13f4`）与 [#80](https://github.com/goodname427/agent-brain-client/pull/80)。
- 新阶段源码 tag：`agent-brain-client-0.5-freeze-20261103`，指向包含本清单与归档的最终合并提交。为避免文件自引用提交哈希，最终完整 SHA、文档 PR 和最终 main CI URL 记录于 annotated tag；用 `git rev-parse 'agent-brain-client-0.5-freeze-20261103^{commit}'` 解析精确源码，远端读回核对 tag 对象及 peeled commit。
- 仅在封板文档 PR 检查通过、无阻碍合入的 review、合入 main 且最终 main Client checks 成功后创建该 tag。若尚未创建或门禁未通过，不能仅凭本文宣称封板操作已完成。创建后不移动或覆盖；后续修复使用新提交、新记录。
- 阶段 tag 用于源码留档，不是安装源，不新增 GitHub Release，不变更 installer latest 或运行渠道。发布流水线已随 0.5.9 纳入，见 [固定发布入口](../../development/release-pipeline.md)；本次不执行发布。

旧 [FREEZE-4.0.json](../../../FREEZE-4.0.json) 及 `agent-brain-product-4.0-*` 标签保持原字节、名称与对象。其中“4.0”是 0.4 阶段历史口误标识，不能改写；本次使用规范的 0.5 名称，不创建“5.0”标签。

## 已发布组件

| 对象 | 固定提交／标识 |
| --- | --- |
| 安装器 0.5.9 | `c898f4f52b1194c2d9c2ea9cbd846af820e4adb8`；[installer-0.5.9-windows-setup](https://github.com/goodname427/agent-brain-client/releases/tag/installer-0.5.9-windows-setup) |
| Windows 运行包 0.5.9 | `db623a805e726e8f1cbc980254c56f3a45bb1e4e`；[client-0.5.9-windows](https://github.com/goodname427/agent-brain-client/releases/tag/client-0.5.9-windows) |
| Windows stable 读取快照 | `refs/heads/releases/personal-https-windows-stable` 指向上述运行提交 |
| manifest | 21 文件；SHA256 `3dca189ee399bdb326272a9ae35ebf10e346460cf218525c1b2898e760ab8eca`；`core_api:1`、`state_schema:1`、`python_min:3.11` |

实际下载现有安装器源码归档、AMD64／ARM64 两运行归档及两个 SHA256SUMS，共五个资产：大小、SHA256 与 GitHub 资产元数据及清单均匹配。运行 Git 提交的 21 文件逐个匹配 manifest；两归档内文件也按 manifest 核对。精确文件名、大小、摘要和固定下载 URL 保存在冻结 JSON。本次不重建、不重传或替换任何资产。

Mac 继续原受信渠道：读取时 `releases/personal-https-stable` 为 `2d78fe676c9173c4976c116080fbb75ea32012b3`（0.3.1），历史 0.4.8 候选 `releases/personal-https-candidate-0.4.0-install-fix-ce9f72b` 为 `6927089fd33ad59b5133d6b8a29277d3c0c4db1e`。其余已有渠道同样保留，不提升、不把 Windows 0.5.9 套用于 Mac。main 文档变化不等于已安装设备运行包变化。

## CI 与验收证据边界

基线 [Client checks 41592591088](https://github.com/goodname427/agent-brain-client/actions/runs/41592591088) 对精确 main `b2e5b9a` 成功；`isolated-client`、`windows-contracts (3.11)`、`windows-contracts (3.13)` 三 job 均成功。最终封板还须通过上文门禁，由 tag 注释保留最终 main CI 入口，不将旧基线结果冒充最终提交检查。

用户已确认本轮 **Windows 安装与验收通过**。设备架构、逐项 installation stage 与 worker／模型加载证据未提供，不补造这些元数据，不外推到其他设备、全部 Hook／模型或异常恢复场景。

此前独立获授权的生产 catalog 核验报告：Windows AMD64 与 ARM64 各 21 文件核验通过。本次只转录该结论，不访问生产 Server 或私有证据，也不将 catalog／包校验当作 ARM64 真机验收。**ARM64 实机未测。** 私有设备与生产日志由所有者保留，不带入公开仓库。

## 已知问题与后续候选

| 项目 | 封板时状态 |
| --- | --- |
| Windows `memory_query`／`content_read` 曾 120 秒超时 | 未解决；根因未知。用户随后确认安装验收，不能据此宣布读取超时已修复 |
| Mac 保存异常 | 未解决；用户要求后续修复，本次无修复或重新验收结论 |
| ARM64 真机 | 未测试；两架构 catalog 与包文件核验不替代硬件测试 |
| onboarding 内容偏长期主题 | 用户要求先不改；保持暂停，列为后续候选 |

这些是带已知问题的 Client 阶段快照，不是零缺陷或所有场景通过的声明。后续按新任务定位问题并记录版本、平台、实际命令与结果；不能改写本次证据来消除历史失败。

## 恢复与后续维护

继续使用现有安装／检查／升级／恢复流程，保留身份、凭据引用、信任锚、来源批准、队列与回执。阶段 tag 不用于绕过安装政策或手改 active 指针；不删除恢复材料，也不倒退数据。后续代码修复和正式发布分别走既有 PR、CI 和授权流程。此次没有部署、修改 Server、重发生产包、提升渠道或变更权限。
