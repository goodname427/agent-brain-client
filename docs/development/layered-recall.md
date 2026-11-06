# L2/L3 recall and delivery evidence

The single MCP `memory_query` and the owned question Hook use the same native
L1 retrieval and independent L2/L3 assembly. They retain committed source
provenance and emit `hookSpecificOutput.additionalContext`.

The optional `limit` is native L1 maxResults **per namespace**, default 5. Native
`recall` options configure strategy, scoreThreshold, maxCharsPerMemory,
maxTotalRecallChars and timeoutMs. Zero character budgets mean unlimited. Counts
do not combine L1 with L2/L3 or cap saved memory. Native FTS/RRF order is preserved;
the Client does not clamp to three. The Server keeps an older caller's combined
count only for wire compatibility when it lacks the native `recall` object.

Both Mac and Windows can persist Hook settings without changing source approval:

```sh
python3 src/agent_brain_client/interfaces/cli/collection.py global-recall-configure --recall-options '{"maxResults":8,"scoreThreshold":0.3,"maxCharsPerMemory":0,"maxTotalRecallChars":0}'
```

This example demonstrates configuration, not a universally recommended count.
Default 5 still misses target memories at native ranks 6/8 in the known failure.
Unavailable embeddings use native FTS fallback; configuration does not activate
new providers, credentials, fees or indexing. Source collection remains all
approved projects going forward, without historical backfill.

The personal pool can return another enrolled device's personal namespace with
`source_device_id`. Previously the Client rejected that additional field and
required every source device and origin to match the reader's local device.
A legitimate peer result therefore failed before context preparation. Both
Mac and Windows use the same implementation:

- Legacy local groups retain local device and directory checks.
- A peer must name the approved personal namespace, personal sharing, and an
  exact device matching every committed provenance source. The authenticated
  Server checks that peer's enrollment and its source approval.
- Peer paths are validated as origin metadata. They do not authorize collection
  from those directories on the reader's computer.
- Transport size bounds and reference-only applicability remain.
  Unknown fields, mismatched devices, project-only peer groups and malformed
  source context fail closed.

`tests/test_layered_recall.py` covers both path directions, valid L2/L3 output,
legacy compatibility, negative scope/provenance cases and bounded empty groups.
It runs in the full Mac suite and Windows contract jobs. The subprocess fixture
`tests/layered_recall_bridge.py` supports an explicit Server integration runner;
it is never part of a release and never activates host credentials.

## Content-free receipts

The question Hook now saves one private `recall-receipts/<request_key>.json`
per attempt as well as the latest summary in `last-recall.json`. The request key
binds the approved scope, origin, session, turn and full question. The wire
request ID is `global_recall_<request_key>`. Schema 2 receipts contain hashes of
the session, turn, actual query, returned and selected record IDs/content, and emitted
context, plus layer/count/byte totals and the number omitted for budget. They
contain no query, memory text, source paths, credentials or exception values.
Later queries cannot overwrite a different request's evidence. Old schema 1
summaries remain readable but cannot establish request identity.

Use `global-status --request-key <request_key>` to inspect the exact attempt.
The matching key is included in the Hook's content-free stderr receipt and its
private prompt stamp filename. Hashes use the existing canonical JSON SHA-256
function in `global_memory_scope.digest`, including for string values.

- `failed`: the attempt failed before preparing context; `error_type` names the
  class only. No successful deduplication stamp is written, so a retry is possible.
- `empty`: the query completed with no injectable results.
- `context-prepared`: source validation and selection completed. Record/content
  hashes establish which selected results belonged to this request.
- `output_written: true` is persisted only after stdout has been flushed. The
  stderr `hook-output-written` receipt carries the same request key and context
  hash. Neither proves host acceptance or model use; `model_used` stays `unknown`.

The Hook fits whole native-selected results to 24 KiB of actual UTF-8 context,
including origin metadata and the policy prefix. It does not measure escaped
ASCII JSON or fail the entire recall because another candidate is too large.
Oversized candidates are omitted with a count; stored memory is never truncated.

The owned Stop Hook is synchronous, with the existing 120-second host timeout
and 110-second client deadline. This lets a short-lived `codex exec --json` wait
for the durable outbox path. An isolated CLI 0.166.0-alpha.5.1 probe with a local
fictional Responses provider observed only UserPromptSubmit with asynchronous
Stop; synchronous Stop received and completed the event before exit. Both runs
included UserPromptSubmit additionalContext in the outgoing model request.
This is host/version-specific evidence, not a guarantee for a host that omits
Stop (including failed/interrupted turns). Such turns retain their staged prompt;
there is no transcript scan, fabricated Stop, or historical backfill. For an
unsupported host use the existing explicit `record` notes path.

Model use needs controlled host acceptance: seed fictional L2/L3 markers in an
isolated test Server, start an independent Codex session on each OS, ask an
unhinted question, and compare the answer with the expected markers. Record
model use only from that evidence. Unit and subprocess tests do not invoke a
model or attest native host behavior.

## Deployment boundary

Source, an active MCP package, and the pinned global Hook can have different
versions. Updating the MCP package alone does not refresh an independently
pinned Hook. Use the normal owned Hook installation/update flow to refresh its
code pins; preserve existing source approvals and host trust settings.

A business recall can admit a session on the Server and update a Client memory
snapshot. It is not operationally write-free even when its persistence envelope
says `read-only`. Investigation under a no-production-write restriction uses
code, existing content-free receipts and isolated synthetic fixtures.

The only daily query tool is `memory_query`; `memory_recall` and catalog retrieval aliases have been removed. Automatic Hooks also send `memory_query`. Direct Agent facts and extracted facts share native L1 after the Server's reviewed migration cutover. New native save receipts carry `storage=tencent-l1` and a checkpoint digest; local persistence does not prove remote backup.

Content inventory uses optional opaque cursor pagination, without adding another
daily memory-query tool. Client refresh follows every page and verifies the
complete snapshot before native mapping. Page size and snapshot byte budgets do
not limit the total number of saved native memory topics.
