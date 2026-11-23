# 文档维护与命名

> 当前维护。适用 Client 仓库，入口核对基线为 Client 0.8.36。

## 一个主题，一份正文

当前安装、架构合同、开发发布、验收分别放入 `installation/`、`reference/`、`development/`、`acceptance/`；版本状态放入 `releases/`，历史记录放入 `archive/<版本>/`。从 [文档索引](README.md) 查找正文。只在权威正文修改内容，分类索引只作导航，不复制正文。

新文件使用小写英文 kebab-case 与 `.md`，目录索引使用 `README.md`。发布说明采用 `releases/<版本>-<主题>.md`，设备验收采用 `acceptance/<版本>-<平台>-<YYYY-MM-DD>.md`。开头标明当前维护／参考／历史、适用版本及核对日期；阶段编号、源码、安装器、活动包、worker 分开记录。历史说明保留当时事实，不将历史“未测”或一次用户验收外推为当前全场景结论。

## 入口与迁移

不为每个旧文件自动保留导读页。迁移时先检查脚本、安装入口、仓内链接及已发布材料；更新可控制的引用，只有确认无法同步迁移的实际依赖才保留必要入口，并注明依赖与移除条件。

面向使用者的入口从根 README、AGENTS 和 BOOTSTRAP 通向正式平台脚本及日常维护。固定包内的 PROTOCOL 和 ADAPTERS 把版本专属的旧步骤链接到对应历史记录。历史适配与协议记录仍可用于核对旧绑定、回执和恢复证据。

根 [README](../README.md)、[AGENTS](../AGENTS.md)、[BOOTSTRAP](../BOOTSTRAP.md) 是实际安装入口，保留完整正文与路径。README、BOOTSTRAP、PROTOCOL、ADAPTERS 还属于固定运行包文件集合，不能为分类移走；包内不含完整 docs 树，仍须在官方源码仓库查阅文档，不扩大包范围。

已发布的 0.5.9 安装器源码 tag 保留迁移前的文件和相对引用；历史 tag／提交的地址不因删除 main 上的导读页而改变。无法从本仓库确认外部是否有人保存过指向 main 旧文件的链接，不宣称这些入链不存在；此类旧 main 地址在删除后不再兼容。当前使用从根安装入口或文档索引开始。

[connection.example.json](connection.example.json) 是无凭据连接格式合同，[源码 manifest](../releases/manifest.json) 记录冻结的 0.3.1 文件合同。两者保留原路径，平台运行包的现行 manifest 在构建时生成。[历史冻结映射](archive/0.4/FREEZE-4.0.json) 和 [0.5 冻结清单](archive/0.5/FREEZE-0.5.json) 保留原字节、文件名和旧 tag，在 main 归入对应阶段。记忆内容、组织、受管 Rule/Skill 与生产 catalog 不属于本次整理范围。

## 检查与交付

运行 `python3 -B dev/checks/docs.py` 检查全部 Markdown 相对链接与标题／显式锚点；它是离线检查，不证明所有外部 URL 或未知入链可用。当前导航使用行内／引用链接与 ATX 标题；新增其他语法须扩展检查器。`tests/test_check_docs.py` 用临时文件注入断链、坏锚点等故障，验证检查确实会失败。手动验证可通过现有 Client checks 工作流运行。

改动前后查引用，更新相关正文与索引；纯 Markdown 改动按 [CI 变更范围](development/ci-scopes.md) 运行文档与轻量回归，不要求 Mac／Windows 全盘测试。涉及代码、安装行为或固定合同数据时，按 [开发入口](development/README.md) 隔离 HOME/CODEX_HOME 运行对应回归、公开扫描、源码与格式检查。固定数据若确需改变，另走合同审查。公开材料不含真实凭据、记忆、私密路径或部署地址。

提交精确文件清单并通过 PR 审核，报告准确提交、测试通过／跳过／未测和 CI。文档合入不触发封版、发版、渠道提升或生产操作；发布遵循 [现有流程](development/release-pipeline.md) 和独立授权。
