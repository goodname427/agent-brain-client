# Client architecture

> Current development design, 2026-11-06. Source work only; no release, device installation or Harness activation is implied.

The Client has five source boundaries: domain contracts, application use cases, concrete adapters, external interfaces, and runtime composition. Developer commands and checkout bootstraps are outside the installed application. There is one implementation of each contract, durable queue algorithm, native mapping transaction and tool catalog.

```text
src/agent_brain_client/
  domain/           content, events, turns, scopes, authorization, receipts
  application/      capture, recall, content management, queue, pull, enrollment, updates
  adapters/
    harnesses/      Codex native files/events; CodeBuddy native files/events
    platforms/      portable filesystem API; Mac Keychain/helpers; Windows native APIs
    server/         HTTPS, SSH and authenticated collection transport
    storage/        pinned metadata, cache files, recovery journals, origin metadata
    packages/       exact package inventory, validation, assembly, release storage
  interfaces/
    cli/            install, install plans, device operations, sources, projects, CodeBuddy
    mcp/            framing, scheduling, content service, optional topic service
    management/     local Rule/Skill management page and HTTP boundary
  runtime/          composition of enrollment, project/source, installation and update ports
bootstrap/          official checkout and interpreter preparation; OS install entries
bin/                normal memory-operation shell entries
dev/                checks, candidate builds, release operations, integration probes
tests/              synthetic contracts, native regressions, architectural fences
```

## Dependency direction

Domain modules have no filesystem, network, native API or application dependency. Application modules import only domain/application code and the standard library. Adapters implement external behavior and may use those inner layers; they cannot import interfaces or runtime composition. Codex and CodeBuddy adapters cannot import each other. Shared storage cannot import either Harness. Interfaces translate requests into these services; runtime modules assemble concrete collaborators. Product code never imports developer tools or checkout bootstraps.

`dev/checks/source.py` enforces direct, qualified, relative and literal dynamic imports. `tests/test_architecture.py` injects forbidden edges and checks that importing shared use cases loads no Harness or platform implementation and leaves import paths unchanged. Direct executable entries establish their package path only when run as scripts. Concrete Mac and Windows modules cannot import each other; platform selection belongs to the portable facade.

## Responsibilities and real call flows

- `domain/contract.py` is the explicit wire-contract facade over separately owned content, event, authorization, scope and receipt rules. It contains no adapters or native instruction policy.
- `application/queue/outbox.py` owns durable write/retry/conflict decisions. `adapters/storage/content.py` owns filesystem enumeration and persistence. Schema-1 row bytes, request IDs, CAS bases and identity bindings keep their meanings.
- `application/installation/enrollment.py` owns preparation, confirmation, restart and terminal-reset decisions. Its typed ports provide transport, credential CAS storage and secure input. `domain/enrollment.py` owns the persisted record contract also used by authenticated requests; HTTPS does not instantiate a coordinator to validate credentials. The earlier unused generic enrollment implementation is removed.
- A Codex content session composes `sources.py` for source review/import, `mapping.py` for owned native files and recovery, and `ContentSynchronization` for pull sequencing. Pulling without replay remains separate from resending a write. Typed refresh ports expose recovery, ownership snapshots and uncertainty; only the mapping adapter reads or interprets `transaction.json`, including offline-read and removal readiness gates. The durable outbox calls the typed source reconciliation port for validation, confirmation and review revocation; only the source owner interprets or writes `imports.json`, including the credential-free removal inventory.
- `application/memory/recall.py` receives a client factory, source gate and cache operations from runtime composition. Current-directory discovery has one implementation in `adapters/storage/origins.py`.
- `application/tools/catalog.py` owns all content-tool schemas, descriptions and grant filtering used by MCP and installation. The installer can inspect a verified package's own offline descriptor; it never substitutes another version's whitelist.
- The management page calls `application/content/management.py`. A confirmed Server save, backup confirmation, native-file synchronization and model loading remain separate results.

Capture follows `native event → Harness parsing → normalized turn use case → bound durable queue → enrolled transport → validated receipt`. Rule/Skill activation follows `CLI/MCP/page → management use case → original request ID and CAS → receipt → synchronization coordinator → native mapper`.

## Scopes and identities

Owner/grant determines which collection and operations are authorized. Personal/project sharing determines provenance and applicability. Device enrollment determines credentials and the queue binding. Directory/worktree metadata determines origin. Session/turn identity determines ordering, deduplication and exact retries. Native global/project configuration determines where a Harness maps approved instructions.

These dimensions do not replace each other. Selecting all future projects does not enlarge a Server grant or authorize historical import. The enrolled `codex` transport fields and persisted `codex_home` field are existing protocol/storage identities; CodeBuddy does not impersonate a new device by renaming them. Its capture queues retain their distinct host binding and namespace.

## Entry consolidation

Build and release commands live only in `dev/build/` and `dev/release/`. Installation lifecycle commands use the platform bootstrap and install CLI. Exact plan preparation/application and read-only inspection use the single `interfaces/cli/install_plan.py` parser. The Codex installer adapter has no executable main or second parser. The existing JSON-line HTTP request command lives at `interfaces/cli/request.py`; its HTTP adapter is import-only. Device task execution uses `interfaces/cli/device.py`; it no longer forwards through a version-named omnibus tool. The former root connection checkpoint is a developer integration probe. The deferred notify experiment is a developer prototype, not an installed capture path.

`interfaces/cli/collection.py` owns global command parsing, stdout/stderr and exit semantics; both memory wrappers and the frozen Codex Hook execute this entry. `application/collection/control.py` owns stop/resume, remote-vs-project decisions, fence lifetime and recall-setting validation through typed ports. Source admission/delegation is shared application logic in `collection/routing.py`; Codex native Hook parsing and bounded file operations live in its collection adapter. `runtime/collection.py` only binds these services for CLI and the authenticated Harness gateway, with no parser or executable main. `interfaces/mcp/launcher.py` likewise owns launcher arguments; the installed MCP command targets it, while the runtime launcher provides the worker startup service. No product parser is permitted outside interfaces.

`interfaces/cli/project.py` owns project command parsing. `runtime/project_memory.py` coordinates approved capture, recall and source operations; `adapters/harnesses/codex/project_configuration.py` owns the project Hook transaction and native context format. The previous project module no longer doubles as a CLI and transport workflow.

Bounded native-source access uses `platforms/tree.py`, with directory-relative POSIX I/O in `platforms/posix/tree.py` and pinned Win32 handles in `platforms/windows/tree.py`. The common verified-file context checks object identity and post-read version. Codex chooses its `memories` tree, source IDs, limits, reviews and import policy; it contains no native open/handle implementation. CodeBuddy transcript opening and canonical path checks use `platforms/source_files.py`; its generation and session interpretation remains in CodeBuddy. Host journal/CAS policy stays in storage; Windows in-place object access and POSIX mode/owner preservation live under their platform directories.

Mac Keychain subprocess handling lives in `platforms/macos/keychain.py`; Win32 metadata, lock creation and shell rendering live in `platforms/windows/filesystem.py`. Shared `platforms/credentials.py` owns credential records and backend selection, while `platforms/filesystem.py` dispatches the portable operations. Request-aware subprocess cancellation belongs to `platforms/processes.py`, shared with the credential helper rather than imported from HTTP.

`adapters/harnesses/codex/launcher_update.py` owns the fixed-launcher transaction for both platforms. `application/installation/update.py` decides when recovery, discovery, activation and follow-up setup may run. `runtime/updates.py` only assembles its native and source-setup ports. The old version-specific 0.3.0 bridge is removed.

New Harness work adds native event/configuration/source behavior under its own adapter and wires it at the runtime boundary. Shared queue, authorization and transport code remain shared. New OS work belongs under platform adapters and bootstrap entries; it must not copy the memory or synchronization workflow.

## Installation and data boundary

This source accepts one exact current executable layout. There are no old source aliases, old-layout inventories or parallel implementations. Older installations may require a new installation; this change does not promise an in-place upgrade from the previous layout. Published tags and the committed historical manifest remain immutable.

Executable-layout compatibility is distinct from data preservation. No memory, profile, credential, source decision, queue, cursor or stop marker is deleted by this source reorganization. Existing enrollment and queue identities must not be silently replaced or transferred to another binding. Installation/uninstallation journals, outside-edit refusal and rollback remain implemented. Real-device reinstallation and retained-data access must be verified independently before deployment; synthetic tests do not establish that a user's installed model or Harness has adopted the new source.

Rule fallback PR #130 remains separate. No new Harness, fallback, grant, source range, Server deployment or release is activated by this refactor.

## Review of the structural change

| Previous responsibility mixture | Current owner and boundary |
| --- | --- |
| Version-named management tool combined planning, installation, updates, builds and tasks | Install-plan CLI, install lifecycle CLI, application update use case and runtime composition, developer build command and device CLI each have one entry |
| Queue decisions, file persistence and Codex native mapping shared one adapter | Application durable queue, shared file storage, Codex source review, native mapping and pull coordinator are separate collaborators |
| Tool lists reconstructed by installer and MCP | One catalog defines tool names, schemas, descriptions and grant filtering; verified foreign packages describe their own contract offline |
| Project module parsed CLI input, installed Hooks and performed capture/recall | Project CLI, runtime orchestration and Codex native project configuration are separate |
| Mac Keychain and Win32 metadata hidden in shared modules | OS implementations are under their own platform directories; shared APIs select them |
| Developer checkpoint and deferred notify experiment looked like installed runtime | Checkpoint is under developer integration; the deferred experiment is under developer prototypes and excluded from every runtime payload |

Some outer workflows remain large: installation coordinates an existing multi-stage transaction, and project memory supports several separately authorized source modes. Those workflows still require careful review when changed. Module size is not evidence of architectural quality; import direction, actual call sites, owned state and exact recovery behavior are the review criteria.

## Review follow-up to the first implementation

Independent review of PR #134 found real gaps in the earlier implementation, despite its passing CI. This follow-up corrects them:

| Before review | Current owner and actual dependency |
| --- | --- |
| Codex inventory implemented POSIX no-follow and Win32 APIs; shared storage contained a Win32 handle bridge | Harness source semantics call the platform tree facade; storage journals call platform host-file operations. Native implementations live in POSIX/Windows modules. |
| Both Codex installer and install-plan interface parsed plan/apply commands | One plan/apply/inspect parser in the interface; every caller uses the adapter's Python operations or that interface. |
| Application queue read/wrote the source ledger and pull decoded mapping recovery journals | Typed source and mapping ports; the source and mapper are the sole state owners. Queue schema and exact request/CAS replay remain application responsibilities. |
| HTTPS owned enrollment state transitions and runtime owned update decisions | Application enrollment/update use cases with typed ports. HTTPS performs transport; the Codex update adapter performs local registration, environment and transaction I/O. |
| `bin/memory.sh` imported a deleted checkout module before dispatch | It imports the current bootstrap; a real shell test verifies project, stop, resume and management dispatch with a synthetic installed interpreter. |

The follow-up also closes offline/removal ledger reads and the global CLI: the source owner inventories removal blockers without activating an identity; the mapping owner reports settlement without attempting recovery. Real wrapper tests now import all actual entry modules and execute Server-stop, uncertain Server-stop and project-stop through the application and real native fence writes, with synthetic enrollment metadata and remote/project control bindings.

The boundary checker now rejects ctypes/fcntl/msvcrt/winreg imports and native handle/no-follow symbols outside platforms, CLI parsers/entries outside interfaces, platform dependencies on higher adapters, and application access to the source/mapping private state and direct Codex journal access outside its owner. Tests inject aliases and string-based native lookups as well as direct violations. Pure HTTPS origin parsing is a domain syntax check through `urllib.parse`; it performs no network I/O.

## Verification boundary

Contract regression covers queues, original request IDs/CAS, source approval and stop fences, content grants, Rule/Skill mapping and recovery, install/remove/reinstall identity retention, immutable package validation, concurrent worker leases, and both Harness integrations using fictional transports and isolated homes. Architecture checks reject forbidden domain/application, storage/Harness, cross-Harness and cross-OS dependencies, native API leakage and duplicate adapter CLIs, and verify the exact runtime inventory. Inner-layer code cannot call filesystem methods directly: capture staging and recall cache existence go through storage ports. Executable compatibility tests for removed historical layouts are deliberately retired; current transaction, concurrency and data-retention tests remain.

Mac helper compilation/self-check does not access Keychain or display UI. Application workflow tests independently verify recovery-before-replay, pending-change mapping gates, owner-reported uncertainty, check-only update behavior, interrupted launcher recovery, unconfirmed activation and managed Hook conflicts. Existing behavioral regressions remain necessary for the actual adapter contracts. Native Windows checks must execute on Windows 3.11 and 3.13; passing portable mocks on Mac is not native Windows acceptance. No real-device installation, historical-memory scan, Server change or publication belongs to this refactor.

The CodeBuddy frozen gateway contains the complete current runtime, with per-file hashes. It refuses a shared-runtime/code mismatch before loading services; rerun the official integration installation to refresh its binding. This avoids importing an unrelated checkout while merely validating another directory.

Windows native CI caught a remaining pre-layout credential import. Its import now names the shared credential module directly; this changes the pinned Python backend source digest. New profiles/packages derive that digest from the current source. No old digest is accepted or rewritten to bypass review. Native credential, ACL, CAS and removal behavior is otherwise unchanged; the Swift sources retain their original bytes.
