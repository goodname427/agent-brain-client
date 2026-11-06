# Client architecture

> Current development design, 2026-11-06. Source work only; no release, device installation or Harness activation is implied.

The Client has five source boundaries: domain contracts, application use cases, concrete adapters, external interfaces, and runtime composition. Developer commands and checkout bootstraps are outside the installed application. There is one implementation of each contract, durable queue algorithm, native mapping transaction and tool catalog.

```text
src/agent_brain_client/
  domain/           content, events, turns, scopes, authorization, receipts
  application/      capture, recall, content management, queue, pull, enrollment
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
  runtime/          enrollment, project/source coordination, installation, launcher updates
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
- A Codex content session composes `sources.py` for source review/import, `mapping.py` for owned native files and recovery, and `ContentSynchronization` for pull sequencing. Pulling without replay remains separate from resending a write.
- `application/memory/recall.py` receives a client factory, source gate and cache operations from runtime composition. Current-directory discovery has one implementation in `adapters/storage/origins.py`.
- `application/tools/catalog.py` owns all content-tool schemas, descriptions and grant filtering used by MCP and installation. The installer can inspect a verified package's own offline descriptor; it never substitutes another version's whitelist.
- The management page calls `application/content/management.py`. A confirmed Server save, backup confirmation, native-file synchronization and model loading remain separate results.

Capture follows `native event → Harness parsing → normalized turn use case → bound durable queue → enrolled transport → validated receipt`. Rule/Skill activation follows `CLI/MCP/page → management use case → original request ID and CAS → receipt → synchronization coordinator → native mapper`.

## Scopes and identities

Owner/grant determines which collection and operations are authorized. Personal/project sharing determines provenance and applicability. Device enrollment determines credentials and the queue binding. Directory/worktree metadata determines origin. Session/turn identity determines ordering, deduplication and exact retries. Native global/project configuration determines where a Harness maps approved instructions.

These dimensions do not replace each other. Selecting all future projects does not enlarge a Server grant or authorize historical import. The enrolled `codex` transport fields and persisted `codex_home` field are existing protocol/storage identities; CodeBuddy does not impersonate a new device by renaming them. Its capture queues retain their distinct host binding and namespace.

## Entry consolidation

Build and release commands live only in `dev/build/` and `dev/release/`. Installation lifecycle commands use the platform bootstrap and install CLI. Exact plan preparation/application uses `interfaces/cli/install_plan.py`. Device task execution uses `interfaces/cli/device.py`; it no longer forwards through a version-named omnibus tool. The former root connection checkpoint is a developer integration probe. The deferred notify experiment is a developer prototype, not an installed capture path.

`interfaces/cli/project.py` owns project command parsing. `runtime/project_memory.py` coordinates approved capture, recall and source operations; `adapters/harnesses/codex/project_configuration.py` owns the project Hook transaction and native context format. The previous project module no longer doubles as a CLI and transport workflow.

Mac Keychain subprocess handling lives in `platforms/macos/keychain.py`; Win32 metadata, lock creation and shell rendering live in `platforms/windows/filesystem.py`. Shared `platforms/credentials.py` owns credential records and backend selection, while `platforms/filesystem.py` dispatches the portable operations. Request-aware subprocess cancellation belongs to `platforms/processes.py`, shared with the credential helper rather than imported from HTTP.

`runtime/launcher_update.py` owns the current fixed-launcher transaction and exact interrupted-update recovery for both platforms. The old version-specific 0.3.0 bridge is removed.

New Harness work adds native event/configuration/source behavior under its own adapter and wires it at the runtime boundary. Shared queue, authorization and transport code remain shared. New OS work belongs under platform adapters and bootstrap entries; it must not copy the memory or synchronization workflow.

## Installation and data boundary

This source accepts one exact current executable layout. There are no old source aliases, old-layout inventories or parallel implementations. Older installations may require a new installation; this change does not promise an in-place upgrade from the previous layout. Published tags and the committed historical manifest remain immutable.

Executable-layout compatibility is distinct from data preservation. No memory, profile, credential, source decision, queue, cursor or stop marker is deleted by this source reorganization. Existing enrollment and queue identities must not be silently replaced or transferred to another binding. Installation/uninstallation journals, outside-edit refusal and rollback remain implemented. Real-device reinstallation and retained-data access must be verified independently before deployment; synthetic tests do not establish that a user's installed model or Harness has adopted the new source.

Rule fallback PR #130 remains separate. No new Harness, fallback, grant, source range, Server deployment or release is activated by this refactor.

## Review of the structural change

| Previous responsibility mixture | Current owner and boundary |
| --- | --- |
| Version-named management tool combined planning, installation, updates, builds and tasks | Install-plan CLI, install lifecycle CLI, runtime update coordinator, developer build command and device CLI each have one entry |
| Queue decisions, file persistence and Codex native mapping shared one adapter | Application durable queue, shared file storage, Codex source review, native mapping and pull coordinator are separate collaborators |
| Tool lists reconstructed by installer and MCP | One catalog defines tool names, schemas, descriptions and grant filtering; verified foreign packages describe their own contract offline |
| Project module parsed CLI input, installed Hooks and performed capture/recall | Project CLI, runtime orchestration and Codex native project configuration are separate |
| Mac Keychain and Win32 metadata hidden in shared modules | OS implementations are under their own platform directories; shared APIs select them |
| Developer checkpoint and deferred notify experiment looked like installed runtime | Checkpoint is under developer integration; the deferred experiment is under developer prototypes and excluded from every runtime payload |

The outer composition modules remain the largest parts: installation coordinates an existing multi-stage transaction, and project memory supports several separately authorized source modes. Those workflows still require careful review when changed. Module size is not evidence of architectural quality; import direction, actual call sites, owned state and exact recovery behavior are the review criteria.

## Verification boundary

Contract regression covers queues, original request IDs/CAS, source approval and stop fences, content grants, Rule/Skill mapping and recovery, install/remove/reinstall identity retention, immutable package validation, concurrent worker leases, and both Harness integrations using fictional transports and isolated homes. Architecture checks reject forbidden domain/application, storage/Harness, cross-Harness and cross-OS dependencies, and verify the exact runtime inventory. Inner-layer code cannot call filesystem methods directly: capture staging and recall cache existence go through storage ports. Executable compatibility tests for removed historical layouts are deliberately retired; current transaction, concurrency and data-retention tests remain.

Mac helper compilation/self-check does not access Keychain or display UI. Native Windows checks must execute on Windows 3.11 and 3.13; passing portable mocks on Mac is not native Windows acceptance. No real-device installation, historical-memory scan, Server change or publication belongs to this refactor.

The CodeBuddy frozen gateway contains the complete current runtime, with per-file hashes. It refuses a shared-runtime/code mismatch before loading services; rerun the official integration installation to refresh its binding. This avoids importing an unrelated checkout while merely validating another directory.

Windows native CI caught a remaining pre-layout credential import. Its import now names the shared credential module directly; this changes the pinned Python backend source digest. New profiles/packages derive that digest from the current source. No old digest is accepted or rewritten to bypass review. Native credential, ACL, CAS and removal behavior is otherwise unchanged; the Swift sources retain their original bytes.
