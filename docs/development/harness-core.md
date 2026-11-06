# Shared Client runtime

> Current development reference; applies to the Client runtime layout cleanup after 0.7. Checked 2026-11-06.

The Client separates reusable runtime libraries from native host behavior, installation and developer commands. See [runtime layout](runtime-layout.md) for directory ownership and upgrade boundaries. Source changes do not publish or install a release.

| Module | Shared responsibility | Host supplies |
| --- | --- | --- |
| `scripts/brain_content.py` | Existing typed content, event, recall and receipt contracts | Native event parsing and policy selection |
| `scripts/brain_http.py` | Existing authenticated HTTP requests, deadlines, cancellation and receipt validation | Endpoint, credential provider, data mode and adapter identity |
| `scripts/brain_memory.py` | Existing schema-1 CAS outbox, write/replay handling and normalized turn submission | Storage, locks, source review, target validation and native mapping |
| `scripts/brain_cache.py` | Private cache storage, operation diagnostics and event-only queue | Exact binding, policy, protected paths and enrolled client |
| `scripts/brain_host_files.py` | Bounded host-file images, journals, atomic writes and in-place recovery | Host paths, desired bytes and journal persistence |

Current callers import `brain_content` and `brain_http` directly. The old `content_contract` and `remote_http` source aliases are removed; there is one contract and one request-local cancellation context. `RemoteClient` retains its enrolled `adapter='codex'` default, and another enrollment passes its own explicit identity. This header does not change device authorization.

## Next Harness integration

Add the repository's `scripts` directory to the runtime import path and import `brain_content`, `brain_http` and `brain_memory` directly. These imports do not load Codex configuration, inspect transcripts, create credentials, or import the Server/Core. Python 3.11+ and the existing Mac/Windows platform backends remain in use.

Normalize a native event into `scope`, `turn`, `phase`, and `content`. The scope contains `cwd`, `project_id`, and `session_id`; `phase` is `prompt` or `final`. The host must resolve and validate its own project/session and recursion behavior before calling `submit_turn`. The shared function validates the existing text/ID contract, stages the prompt/final pair, and submits through the adapter's original outbox. Supply the host's existing enable predicate, lock implementation, optional clock, and optional current-source predicate. It never discovers native events or injects model context.

For `ContentOutbox`, supply the same storage and policy hooks used by the Codex subclass: `cache`, `platform`, `policy`, `queue_sha`, `client`, `_load`, `_save`, `_safe`, `methods`, `_allows`, `_assert_sources_shareable`, `_imports`, `_import_same`, `_validate_target`, `_read_target`, and the three `_outbox_*` locking/diagnostic hooks. The host keeps responsibility for forming its service/principal/native binding and cache path. Separate Harness installations must use distinct bindings and caches; the shared queue never substitutes a new binding for an existing queue. A host with no native-source imports must explicitly reject nonempty source reconciliation references.

Codex retains native memory inventory, `CODEX_HOME`, TOML installation, native Rule/Skill mapping, source review, Hook parsing and context formatting. Its existing `policy_sha`, `queue_sha`, event/request IDs, queue rows and retained CAS requests are unchanged. No historical capture is added and no Tencent memory implementation is rewritten.

## Package and upgrade compatibility

Current content packages contain 23 SSH files or 25 HTTPS files on either OS. Exact historical memberships, including pre-shared and 0.7 layouts, remain metadata in `brain_layout.py`; published manifests remain immutable. An official source checkout also accepts the exact already released 0.7 layout for discovery, install and update until a new package is published; mixed layouts are rejected. New builds produce only the current layout and report both added and removed paths. The release tool accepts only exact known old-to-current transitions, retaining origin, anchor, capability, credential-source and protocol checks. Unknown extra or missing files fail validation.

An old fixed launcher cannot discover relocated modules. Shipping this source requires the official checkout update path and existing journaled launcher handover; source merge alone is not publication. Read-only inspection selects the launcher inside the verified historical package. Hook refresh recognizes both historical pin families, preserves source decisions, stop state, pending data and request IDs, and freezes the current runtime before changing its owned command. Only runtime files are frozen; installation and operator configuration modules stay in the source installer. This also keeps the frozen runtime readable by existing CodeBuddy loaders. The new Hook requires its complete current pin set before execution.

## Validation

`test_harness_core` exercises shared-only imports, a fictional host with no Codex adapter, explicit HTTP identity, cancellation aliasing, exact event/request IDs, restart/replay and changed-request refusal, and the narrowly defined release dependency transition. Existing event, outbox, HTTP, installation, Windows contract and offline recovery tests continue to exercise the Codex boundary. Synthetic checks do not assert that CodeBuddy has been installed or that a physical Windows device was tested.

## Native responsibilities

Codex retains TOML ownership, native memory inventory/review, Rule/Skill mapping, Hook parsing and context formatting. Project-scoped Codex memory automation now lives in `adapters/codex/project_memory.py`; its former tools entry is removed. CodeBuddy uses `EventQueue` directly and never loads Codex mapping merely to stage or replay an event. Its binding still uses the original enrolled runtime path fields, preserving existing queue hashes and requests.
