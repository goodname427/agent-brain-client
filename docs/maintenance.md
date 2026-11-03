# 文档维护与命名

> 当前维护；适用 Client 仓库。核对日期：2026-11-03，安装器／Windows 基线 0.5.9。

## 一个主题，一份正文

当前安装、架构合同、开发发布、验收分别放入 `installation/`、`reference/`、`development/`、`acceptance/`；版本状态放入 `releases/`，历史记录放入 `archive/<版本>/`。从 [文档索引](README.md) 查找正文。只在权威正文修改内容，分类索引只作导航，不复制正文。

新文件使用小写英文 kebab-case 与 `.md`，目录索引使用 `README.md`。发布说明采用 `releases/<版本>-<主题>.md`，设备验收采用 `acceptance/<版本>-<平台>-<YYYY-MM-DD>.md`。开头标明当前维护／参考／历史、适用版本及核对日期；产品编号、源码、安装器、活动包、worker 分开记录。历史说明保留当时事实，不将历史“未测”或一次用户验收外推为当前全场景结论。

## 旧地址与机器入口

迁移前已检索全仓（含安装脚本、包文件清单、测试与隐藏工作流）：没有生产代码解析 docs Markdown 正文。`tools/windows-python.ps1` 只在错误提示中给出旧 Windows 文档路径，因此旧地址保留能直接打开的简短导读，并链接完整新正文，无需修改产品脚本。

22 个旧 docs 地址保留原一级标题和章节锚点，每个旧章节链接到新正文同一锚点；这些兼容页只说明唯一正文位置，不能复制操作步骤。外部使用旧 GitHub／raw 地址的人和 Agent 可以继续找到新正文。未来移动文件时同样保留旧地址和原锚点，并更新内部引用直接指向新正文，避免兼容页串联。

根 [README](../README.md)、[AGENTS](../AGENTS.md)、[BOOTSTRAP](../BOOTSTRAP.md) 保留完整安装入口与路由；AGENTS／BOOTSTRAP 已直接引用分类后的完整安装步骤，自动安装不经过兼容页。README、BOOTSTRAP、PROTOCOL、ADAPTERS 还属于固定运行包文件集合，不能为分类移走；包内不含完整 docs 树，仍须在官方源码仓库查阅链接，不扩大包范围。

[connection.example.json](connection.example.json) 是无凭据连接格式合同，根 [FREEZE-4.0.json](../FREEZE-4.0.json) 是既有不可变基线，[运行 manifest](../releases/manifest.json) 是既有数据，三者保留路径和原字节，不归档、不因文案更新改值。记忆内容、组织、受管 Rule/Skill 与生产 catalog 不属于本次整理范围。

## 检查与交付

运行 `python3 -B tools/check_docs.py` 检查全仓 Markdown 相对链接、标题／显式锚点与兼容页链接；它是离线链接检查，不联网证明外部 URL 可访问。当前导航使用行内／引用链接与 ATX 标题；新增其他语法须扩展检查器。`tests/test_check_docs.py` 通过临时文件注入断链、坏锚点等故障，验证检查确实会失败。现有 Client checks CI 自动运行两者。

改动前后查引用，更新相关正文与索引；涉及安装入口或包内根文档时，按 [开发入口](development/README.md) 隔离 HOME/CODEX_HOME 运行回归、公开扫描、源码与格式检查。固定数据若确需改变，另走合同审查，不能靠刷新摘要消除失败。公开材料不含真实凭据、记忆、私密路径或部署地址。

提交精确文件清单并通过 draft PR 审核，报告准确提交、测试通过／跳过／未测和 CI。文档合入不触发封版、发版、渠道提升或生产操作；发布遵循 [现有流程](development/release-pipeline.md) 和独立授权。
