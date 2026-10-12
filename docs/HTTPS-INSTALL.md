# HTTPS 安装事务与恢复

独立 Client 的 0.2.5 已完成现有设备验收；本说明记录沿用的安装事务。`main` 是源码，不能作为可执行安装渠道。现有 SSH 0.2.1 与 `releases/personal-stable` 保持有效；HTTPS 必须使用单独审核的 Git origin、完整 ref、anchor commit、21 文件 manifest 和 SHA 固定的私有运行政策。

安装前准备一个审核包：21 文件 HTTPS 审核版本、单独固定字节及 Swift 源码摘要的普通 login Keychain helper、无凭据的私有 HTTPS profile、覆盖当前与未来 Memory/Rule/Skill 的 personal-owner read/put 内容政策（旧 31 项精确授权保持兼容）、固定发布政策，以及 `content_install.py plan` 产生的原生配置前像计划。helper 固定在设备本地稳定路径，不随普通包升级替换。个人端点和路径只留本机；计划、profile、政策都是 0600。

审核完成后只运行一个入口。以下变量都是本机审核材料路径或摘要，不是配对码或 token：

```sh
python3 -I -B "$PACKAGE/adapters/codex/https_content.py" install \
  --content-install-plan "$INSTALL_PLAN" \
  --approved-plan-sha256 "$INSTALL_PLAN_SHA256"
```

入口先验证包、政策、计划和原生配置前像，再通过 helper 的安全文本输入窗口接收一次短期邀请。邀请、持久 nonce 与设备凭据只保存在专用普通 Keychain 项及 helper 私有管道；确认后移除邀请。配对成功后执行原有安装事务：检查协议、写入一个 MCP 表和登记回执、再检查协议。任何后像漂移停止恢复，不覆盖其他会话修改。凭据不会因安装回滚丢失，重试继续已有配对；完成后重复同一计划只检查安装，不能重新输入邀请或生成新授权。

启动与工具边界会同步获授权的 memory、rule、skill。离线写入先进入持久 outbox，恢复连接后重放原 request ID 和 CAS revision；冲突保留待人工协调。同步不运行收到的技能脚本。仅隔离验收使用的 0.2.2→0.2.3 升级必须固定原发布来源、范围及 helper 源码，不修改 Keychain 项、原生 MCP 注册或 outbox。

原有安装事务适用于 darwin-arm64、已有 Python 3.11+ 和 Git 的 Mac。首次安装候选的随包依赖准备见 [FIRST-INSTALL-MAC.md](FIRST-INSTALL-MAC.md)。构建 helper 需要 Swift 工具链，最终用户接收审核二进制；不要求用户在安装时编译。首次安装候选的真实依赖配送、下载隔离属性/Gatekeeper、另一台裸 Mac 的真实 Keychain/UI/MCP 加载仍需实际验收，不能以合成测试或 helper self-check 声称完成。没有创建 Apple 身份、Team 或 provisioning。

合成跨仓验收脚本在独立 Server 的 `tests/integration/https_acceptance.py`。它使用真实协议、临时 Git/Data、隔离 HOME/CODEX_HOME、模拟安全输入和 OS 凭据后端；覆盖一次输入、三类读写映射、离线补同步、提交成功后丢失回执的幂等重放以及保持凭据的 0.2.3 升级。它没有打开公网监听或测试真实 TLS/Keychain。
