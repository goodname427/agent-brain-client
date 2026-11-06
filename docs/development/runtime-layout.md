# Client runtime layout

> Current development reference; source cleanup after 0.7. Checked 2026-11-06. No release or device installation is performed by this change.

| Location | Responsibility |
| --- | --- |
| `scripts/brain_*.py` | Shared protocol, HTTP, outbox, private cache, host-file transactions, platform primitives and historical layout identities |
| `scripts/client_release.py`, `client_startup.py` | Package policy, immutable runtime selection and worker handoff |
| `scripts/https_content.py`, `ssh_content.py`, `client_pairing.py`, `system_credentials.py`, `remote_mcp.py` | Existing enrolled transports, credential access, pairing state machine and stdio framing |
| `scripts/global_memory.py`, `global_memory_scope.py` | Existing enrolled global-memory coordinator, source admission, transport, recall and routing to the native Hook boundary |
| `adapters/codex/` | TOML ownership, native memory inventory/review, Rule/Skill mapping, Codex events and project-scoped automation |
| `adapters/codebuddy/` | CodeBuddy JSONC ownership, event identity, transcript cursor, batching and context delivery |
| `installer/` | Shared first install, access input, dependency preparation, recovery, lifecycle and owned global Hook setup |
| `tools/` | Primary user command entries, platform shell entries, build/release tools and developer checks |

`installer/first_install.py` replaces the misleading `tools/mac_first_install.py` name and continues to serve both platforms. The primary documented `install-mac.sh`, `install-windows.ps1`, CodeBuddy installation and memory-management commands dispatch directly to the new owners. They are current public commands, rather than forwarding copies of removed Python modules. The explicit HTTPS enrollment/task CLI is `tools/device.py`; enrollment-plus-native-install wiring lives in `installer/content_enrollment.py`.

There are no old source-path wrappers for moved modules. Imports and repository-controlled generated commands move together. Historical tags keep their original files. Existing immutable packages are inspected at their own paths and upgraded with the existing launcher and owned-Hook transactions; this is data/upgrade support, not a second current implementation. `brain_layout.py` contains only exact historical filename tuples. New builds and frozen Hooks have one new layout. An official source checkout still installs the exact already released 0.7 package while the release channel is unchanged; it selects that package's own verified launcher, then accepts a future complete current package through the normal update transaction. Mixed old/new package memberships are refused.

CodeBuddy's event queue uses shared `StoredContentOutbox`/`EventQueue`, while Codex native mapping subclasses the same storage. One `ContentOutbox.invoke_write` owns retry, stop gating and receipt semantics. Event-only queues reject native reconciliation references. Existing binding keys, queue hashes, schema-1 rows, retained CAS bases and request IDs are unchanged. Directory reorganization does not reset identities, grants, caches, cursors, pending writes or stop fences.

The global-memory coordinator still routes Codex Hook payloads to Codex's native implementation and starts the existing verified backend. This is an explicit host orchestration boundary; the shared leaf libraries do not import that coordinator. `check_source.py` rejects shared-leaf imports of adapters, installer modules or developer tools, including qualified and literal dynamic imports, and rejects CodeBuddy's direct dependency on Codex mapping/file transactions.

Actions and local checks both execute `tools/client_checks.py` and its `COMMANDS` inventory. This includes CodeBuddy lint, Windows CodeBuddy regressions, both platform installation entry parsers, shared runtime type checks and dependency fences. Focused Mac CI substitutes only test selection and skips unchanged Swift helpers; it cannot emit complete local-release evidence. Native Windows runs retain Python 3.11 and 3.13. All test commands isolate HOME, USERPROFILE and CODEX_HOME.

Regression coverage includes shared-only imports without host directories, old pending-event bytes with a closed gate and exact replay after restart, historical policy memberships, frozen Hook refresh, launcher handover, outside edits, interruptions and rollback. Synthetic checks do not establish real CodeBuddy IDE/CLI onboarding or model loading; those results must be checked separately from the user's actual installation.
