# Windows installation and future memory sources

> 状态：当前维护。适用：Windows 0.5.8 起的分阶段安装合同，0.5.9 保留。文档核对日期：2026-11-03；验收范围见 [0.5 交付状态](../releases/0.5-status.md)。

Use the normal public entry from a fresh official Client checkout:

```powershell
& ./bootstrap/windows/install.ps1
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
- Future sources: install and update automatically enable future sessions from
  every project on this device, using its existing HTTPS identity and service.
  Setup reads the authenticated pool identity before registration. It does not
  scan history, import past sessions, or require repeated digest approvals.

Existing exclusions, project isolation, identities, data and queues are retained.
Connection or registration failures preserve the current capture configuration.
Rerun the same install/update entry to recover; a lost response uses the saved
packet and authenticated source readback. No SSH operator or manual profile ID is
required. Collection success and software update success are reported separately.

`installation_complete` remains false until host loading can be observed;
`local_setup_complete` describes the finite local stages only. The CLI does not
claim end-to-end model delivery.

## Resuming a retained Codex registration

The shared installation can finish before the Codex profile has an MCP
binding. Report the shared registration, native MCP stage and worker observation
separately. Rerun the normal official Windows install or update entry to finish
an incomplete binding under the same Windows account. Check the running worker
with a fresh MCP response after the binding is available.

The structural reinstall introduced in Client 0.8.11 could restore the Codex cache before creating the private registration
directory. Under a Windows token that defaults to the Administrators group as
owner, the cache could create the parent with that owner. The first reported
device observation was 0.8.25. [Client PR #148](https://github.com/goodname427/agent-brain-client/pull/148)

Windows now creates the private parent with the
account SID before restoring the cache. For an authenticated completed
reinstall with only that legacy cache in the Administrators-owned parent,
registration resumes in a separate account-owned directory. The old cache,
archive and their security descriptors stay in place. An unexpected owner,
effective access for another principal, or unexpected files in that parent
block this automatic recovery.

## Normal uninstall and reinstall

Pause only Agent Brain MCP in Codex and use its normal reload; wait for its worker
to exit. Then run:

```powershell
& ./bootstrap/windows/install.ps1 -Action uninstall
& ./bootstrap/windows/install.ps1
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
