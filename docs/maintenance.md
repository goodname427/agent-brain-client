# 文档维护与命名规则

> 状态：当前维护；适用整个 Client 仓库。建立日期：2026-11-03，基线为安装器／Windows Client 0.5.9。

## 分类与权威入口

根 README 只保留当前状态、安装路由、核心边界与开发入口；AGENTS 和 BOOTSTRAP 是执行 Agent 的安装入口合同。安装文档保存可直接执行的完整平台步骤；协议、平台边界、发布流程和验收记录各有明确入口，见 [文档索引](README.md)。

新文档按内容进入 `installation/`、`reference/`、`development/`、`acceptance/`、`releases/`；历史证据进入 `archive/<原版本>/`。新文件使用小写英文 kebab-case 与 `.md`，分类索引使用 `README.md`。发布记录采用 `releases/<版本>-<主题>.md`，验收记录采用 `acceptance/<版本>-<平台>-<YYYY-MM-DD>.md`。产品编号和组件版本必须分别写明。

已有大写路径是兼容例外，不为统一大小写机械改名。安装、架构与合同等现有正文留在原地址，分类目录只作索引。归档的新路径使用统一命名；历史旧路径保留完整内容及醒目标记，不能只留下“已移动”或依赖网络重定向。新内部链接优先指向当前正文或归档新地址。

## 状态与版本

每篇新增／实质更新文档在开头写状态（当前维护、参考或历史归档）、适用组件／产品版本、平台和核对日期。历史发布正文保留其原始语境，由归档说明标注版本；不将当时“未测”改成后来通过，也不把用户一次验收推定为全部架构验收。

当前状态统一登记在 [0.5 状态](releases/0.5-status.md)，而非在多篇文档重复声称“最新版”。分别标明源码提交、安装器、渠道活动包和实际 worker；源码 main 与本地 manifest 不替代受信渠道。状态变更同步 README 摘要、分类索引和相关平台正文，旧发布说明不改写为当前操作说明。

记忆政策由现有 Skill 定义；本轮不调整内容记忆组织、受管内容或生产 catalog。公开文档不放入真实凭据、用户记忆、私密路径／主机或部署材料。

## 路径与机器读取兼容

整理前已检索整个仓库（含隐藏工作流）的 Markdown 路径、文件读取、包文件清单、安装脚本、测试和 JSON 引用。已发现：

- AGENTS／BOOTSTRAP／README 与文档之间的安装路由，及 `tools/windows-python.ps1` 的 Windows 文档提示，均保留有效原地址及完整正文。
- `adapters/codex/client_release.py` 的固定包清单包含 README、BOOTSTRAP、PROTOCOL、ADAPTERS；这些根文件不能移走。运行包不含完整 docs 树；包内链接需在官方源码仓库查阅，本次不扩大发行文件／能力集合。
- 本次未发现其他生产代码解析 docs Markdown 正文；外部链接无法穷尽，故全部 22 个旧 docs Markdown 入口保留，历史原有标题锚点和正文保留。新增横幅采用引用块，不改旧标题。
- `docs/connection.example.json` 是旧连接格式契约；`FREEZE-4.0.json` 是既有不可变基线；`releases/manifest.json` 是既有数据。本轮三者逐字节不变，不能移动到 archive、填入真实值或为文案更新摘要。

[documentation-contract.json](documentation-contract.json) 登记旧入口、历史全文镜像与固定数据 SHA-256；`tools/check_docs.py` 验证路径、历史正文等价、相对链接与标题锚点，并拒绝没有索引归属的文档。未来确需迁移时先查代码和外部消费，保留兼容正文与原锚点；数据合同改变必须另行审查，不能仅刷新摘要来消除失败。

## 每次变更的检查

1. 确定唯一权威正文与适用版本，更新相关分类索引；变成历史的说明进入对应归档并保留旧入口全文。
2. 运行 `python3 -B tools/check_docs.py`。检查器仅处理仓库内 Markdown 相对链接／锚点和登记合同，不联网验证外部 URL，也不把 fenced code 中的示例路径当文档链接。当前文档格式是行内／引用链接与 ATX 标题；新增其他 Markdown／HTML 导航语法须同时扩展检查器。
3. 隔离 HOME/CODEX_HOME，运行根 README 的 public scan、source check 与适用合同测试；涉及安装／包内根文档时运行全量标准库回归，Windows 原生项由对应 CI 执行。用 `git diff --check` 检查格式。
4. 审查精确 diff，只提交明确文件清单；报告通过、跳过、未测及 CI 的准确提交。独立 draft PR 交统筹审核合并。文档合入不触发封版、发版或生产操作。

[开发入口](development/README.md) 给出可复现命令；具体发布仍遵循 [发布流程](RELEASE-PIPELINE.md) 及独立授权。
