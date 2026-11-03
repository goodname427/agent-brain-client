# Windows installation and future memory sources

> 状态：当前维护。适用：Windows 0.5.8 起的分阶段安装合同，0.5.9 保留。文档核对日期：2026-11-03；验收范围见 [0.5 交付状态](releases/0.5-status.md)。

Use the normal public entry from a fresh official Client checkout:

```powershell
& ./tools/install-windows.ps1
```

The Windows entry reuses an existing device identity and release policy. It checks
and hands the fixed MCP launcher to the trusted channel package even when the
active package already matches that channel. It does not replace credentials,
expand release scope, reset trust metadata or unpause a disabled MCP service.
If a handover is interrupted, rerun the same install/update entry; keep its private
plan and receipt. Check-update alone does not change the registered launcher.

The result includes `launcher_update` and independent `installation_stages`:

- MCP registration and launcher: exact policy, executable, tool whitelist and
  registration verification. An export without a recorded version reports its
  launcher version as unknown; its package path and authenticated file digests
  still identify it.
- Native mapping: a finite refresh writes enabled Rules into the actual
  `CODEX_HOME/AGENTS.md` and Skills under the device home's `.agents/skills`.
  User content is retained. A mapping failure stays visible in the installer and
  MCP initialize result, even if tools are available.
- Worker and model context: reload only Agent Brain through normal Codex controls,
  then start a fresh conversation to load Rules/Skills. Mapped files and a tool
  list do not prove that a running worker or model has reloaded them.
- Global Hook: the owned inline block is installed in `config.toml`. Other Hooks,
  MCP registrations and legacy `hooks.json` are preserved. Complete normal Codex
  Hook trust review; the installer does not mark its own Hook trusted.
- Future sources: choose `Choose sources` in the local installer, enter project
  roots, exclusions and non-project roots, then approve the displayed scope with
  `Approve`. `Later`, EOF or cancel leaves collection disabled. No history,
  transcript, memory body or project tree is scanned by this setup flow.

Source approval belongs to this device. A prior Mac approval is not a Windows
approval. Explicit local approval creates a private metadata packet. The existing
Server must admit that exact device and scope before activation. If admission is
missing, the result gives the packet path and exact activation command for after
the existing Server operator applies it. This Client cannot grant itself Server
administration. A missing approval is a pending setup stage, not a Hook execution
failure. Already paused settings remain paused across installer reentry.

`installation_complete` remains false until host loading can be observed;
`local_setup_complete` describes the finite local stages only. The CLI does not
claim end-to-end model delivery.

## Normal uninstall and reinstall

Pause only Agent Brain MCP in Codex and use its normal reload; wait for its worker
to exit. Then run:

```powershell
& ./tools/install-windows.ps1 -Action uninstall
& ./tools/install-windows.ps1
```

Normal uninstall removes only authenticated MCP/Rule/Skill mappings and the owned
inline global Hook. It retains credentials, identity, profiles, source settings,
pause markers, outbox, snapshots and recovery receipts. Reinstall reuses those
materials and returns to the staged flow. Do not delete client state, reset ACLs,
copy another device's memory or edit registration digests.

Existing Windows host files retain their file object and security descriptor.
Private cache keys, device metadata and write-ahead receipts keep their existing
private policy. Recognized partial data writes and partial rollback can resume;
unknown edits or replaced objects are preserved for review. This is not a claim
of hardware power-loss immunity.
