# Shared Client runtime

> Current development reference; applies to the Client 0.7 preparation. Checked 2026-11-05.

The Client now separates three reusable modules from Codex's native adapter. This is source preparation for Client 0.7; it does not publish a version or install another Harness.

| Module | Shared responsibility | Host supplies |
| --- | --- | --- |
| `scripts/brain_content.py` | Existing typed content, event, recall and receipt contracts | Native event parsing and policy selection |
| `scripts/brain_http.py` | Existing authenticated HTTP requests, deadlines, cancellation and receipt validation | Endpoint, credential provider, data mode and adapter identity |
| `scripts/brain_memory.py` | Existing schema-1 CAS outbox and normalized prompt/final event submission | Storage, locks, source review, target validation and native mapping |

`adapters/codex/content_contract.py` and `remote_http.py` remain compatibility imports of the same module objects. Existing callers and request cancellation share one implementation and request-local state. `RemoteClient` keeps `adapter='codex'` as its default; another Harness passes its own explicit identity. This header does not enroll a device, alter its grant, or change authentication.

## Next Harness integration

Add the repository's `scripts` directory to the runtime import path and import `brain_content`, `brain_http` and `brain_memory` directly. These imports do not load Codex configuration, inspect transcripts, create credentials, or import the Server/Core. Python 3.11+ and the existing Mac/Windows platform backends remain in use.

Normalize a native event into `scope`, `turn`, `phase`, and `content`. The scope contains `cwd`, `project_id`, and `session_id`; `phase` is `prompt` or `final`. The host must resolve and validate its own project/session and recursion behavior before calling `submit_turn`. The shared function validates the existing text/ID contract, stages the prompt/final pair, and submits through the adapter's original outbox. Supply the host's existing enable predicate, lock implementation, optional clock, and optional current-source predicate. It never discovers native events or injects model context.

For `ContentOutbox`, supply the same storage and policy hooks used by the Codex subclass: `cache`, `platform`, `policy`, `queue_sha`, `client`, `_load`, `_save`, `_safe`, `methods`, `_allows`, `_assert_sources_shareable`, `_imports`, `_import_same`, `_validate_target`, `_read_target`, and the three `_outbox_*` locking/diagnostic hooks. The host keeps responsibility for forming its service/principal/native binding and cache path. Separate Harness installations must use distinct bindings and caches; the shared queue never substitutes a new binding for an existing queue. A host with no native-source imports must explicitly reject nonempty source reconciliation references.

Codex retains native memory inventory, `CODEX_HOME`, TOML installation, native Rule/Skill mapping, source review, Hook parsing and context formatting. Its existing `policy_sha`, `queue_sha`, event/request IDs, queue rows and retained CAS requests are unchanged. No historical capture is added and no Tencent memory implementation is rewritten.

## Package and upgrade compatibility

The split adds exactly `scripts/brain_content.py`, `scripts/brain_http.py`, and `scripts/brain_memory.py` to content packages; the synthetic bridge needs only the first two. Current content packages have 22 SSH files and 24 HTTPS files on Mac and Windows. Published manifests remain immutable.

The new release code recognizes the exact historical package/policy lists and the exact shared dependencies. Existing on-disk policy bytes, origin, anchor, capabilities, credential component and cache bindings remain unchanged. Unknown additional files, removed files, and credential/capability changes still fail existing validation. Historical active packages remain inspectable; new packages include the shared files, hash them in their manifest and pass the same package health check. The release tool permits only the known old-to-new file-set transition.

An already running old launcher cannot import new dependencies from an old package. Use the official source checkout update path and the existing owned launcher handover when shipping 0.7; source merge alone is not publication or installation. Hook code freezing now includes all three shared dependencies. The normal owned Hook refresh recognizes historical pin sets for metadata/recovery, then writes a complete new pin set without changing source decisions, stop state or pending data. The new runtime requires the complete current pin set before execution.

## Validation

`test_harness_core` exercises shared-only imports, a fictional host with no Codex adapter, explicit HTTP identity, cancellation aliasing, exact event/request IDs, restart/replay and changed-request refusal, and the narrowly defined release dependency transition. Existing event, outbox, HTTP, installation, Windows contract and offline recovery tests continue to exercise the Codex boundary. Synthetic checks do not assert that CodeBuddy has been installed or that a physical Windows device was tested.
