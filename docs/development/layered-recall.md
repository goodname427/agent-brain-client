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

The shared question Hook validates each complete source group before adding
any of its rows. A rejected group leaves independently verified groups
available. A response with only rejected groups and no usable rows fails the
recall. A source verification failure preserves the previous offline snapshot.
The receipt sets `retrieval.partial` and `retrieval.source_verification_failed`
without recording rejected sources or their hashes.

An authenticated, successful recall response may carry the top-level boolean
`recall_partial: true` beside `result` when the Server trims its response to fit
its budget. The Hook records `retrieval.partial` and
`retrieval.server_budget_trimmed`. Both reason flags can appear on the same
receipt. Verified rows still follow normal online context selection. Both
partial outcomes preserve any previous offline snapshot and leave the cache
empty if there is no snapshot. The reason flags are confined to the local
diagnostic receipt. Complete recalls retain the earlier schema 2 shape, and
earlier partial receipts remain readable. The offline snapshot is eligible
after a retryable transport failure. An explicit authorization denial leaves
the snapshot unread.

`tests/test_layered_recall.py` covers both path directions, valid L2/L3 output,
legacy compatibility, negative scope/provenance cases and bounded empty groups.
It runs in the full Mac suite and Windows contract jobs. The subprocess fixture
`tests/layered_recall_bridge.py` supports an explicit Server integration runner;
it is never part of a release and never activates host credentials. Enable
`final_context` in its test configuration to exchange the status and query
frames with a separately running Server fixture. Its `hook_output` is suitable
for replay with the native model-request probe's `--client-output` option.

## Server final context for automatic recall

The automatic Hook first reads capabilities from authenticated
`memory_global_status`. With `final-context-v1`, it requests a host-specific
UTF-8 budget and emits only the versioned final body, after checking its
digest, length, source namespaces, devices, origins and committed proof. A
malformed or unsupported final response yields no Hook context. Servers that
lack the capability still return candidates through the compatibility path
described below. Manual MCP queries continue to use the candidate contract.
The existing separately pinned project recall route has its own request contract.

Automatic capture records a normalized Git remote identity at observation
time when one is available. The location and scope in the existing outbox
still determine authorization and the request ID. Onboarding uses the same L0
request, queue, admission and receipt path. A Codex rollout summary can
carry a host-written working directory header. The Client adds a project ID
when that directory resolves inside the approved destination's workspace.
Aggregate Codex memory, other-workspace summaries and CodeBuddy project
cache paths that lose directory separators keep their native-source
fingerprints and approved delivery origin. Missing project identity does
not stop upload. The Client never assigns semantic project objects.

To reproduce model-request measurements for #292, use the public CLI of each
host and the isolated loopback fixture. The fixture runs the Client's shared
Hook output function and checks the decoded request that would reach the
model. The loopback endpoint supplies the synthetic response.

```sh
python3 dev/integration/hook_context.py --host codex --executable /path/to/codex --bytes 10000 10001 --cjk
python3 dev/integration/hook_context.py --host codebuddy --executable /path/to/codebuddy --bytes 24576 125000 --cjk
```

On Mac, Codex CLI 0.171.0-alpha.13.2 preserved 10,000 bytes and dropped the
middle evidence at 10,001. CodeBuddy Code CLI 2.187.0 preserved 125,000 bytes
inside its reminder wrapper. The request budgets are 10,000 and 24,576 bytes
respectively because the wire protocol caps the latter. Use
`--server-example` in place of `--bytes … --cjk` to replay the 465-byte Server
contract fixture. Both Mac CLIs preserved its body digest and Project B
evidence in their model requests. Windows, CodeBuddy IDE, real model
answers and the joint Server/Client quality and token comparison require
separate verification. [Server #188](https://github.com/goodname427/agent-brain-server/pull/188)

## Compatibility context selection

The Hook selects and presents evidence from its existing authorized
`memory_query` result on every turn, regardless of the candidates' total size.
It alternates the strongest local textual matches and native ranked hits,
preserving native relative order. Distinguishing term coverage and current-source
applicability order the strongest textual matches. Derived L2/L3 without a
literal match stay eligible and retain their unranked relevance label. Exact
text duplicates with the same sharing scope and source context contribute once,
preferring the native ranked copy when available. The Core provides native L1
rank and no matched passage. It supplies no query ranks for L2/L3.

Short records can be written whole in evidence order. A long record with a
locatable anchor can use a bounded verbatim `excerpt` containing the adjacent
text before and after the hit, including across a blank line. Its
`excerpt_omissions` flags mark which sides of the source were omitted. For a
long native hit without a locatable anchor, the same evidence pass can use a
verbatim closing excerpt within the 420-character bound if it can keep the
last two source units together. The closing passage follows the native record
rank and may miss relevant text elsewhere in the record. Long records without
an excerpt that fits can use an `index` with an empty answer body. Indexes use
an authorized scenario's readable file name when available,
and otherwise show a record label. The description can contain a labeled,
bounded original-text preview for navigation. The preview is omitted for
private text, insufficient context or insufficient budget, and the index
can show an explanatory notice. Internal paths stay out of the presentation.
Each compact form retains source context and a record digest.
The existing `memory_query` remains available under its normal origin and
authorization. The digest is a correlation reference, and neither the digest
nor a scenario path is an exact read selector in the current Client grant.

The final serialization is limited to 24 KiB of UTF-8 including JSON row
metadata, the policy prefix and separators. Complete short records and
excerpts get space before indexes. The Server's 240,000-byte envelope and
native candidate limits stay independent. The Hook makes one wire request
and formats only the candidates returned by it. The approved offline fallback
keeps the selected presentation and refits any added offline metadata. There
is one cache slot per origin. Offline fallback requires its query, scope and
presentation mode to match the request. A newer lookup in the other mode
replaces the slot. Older untagged caches are accepted only if they contain
whole rows, which the current request formats in its own mode.

Client 0.8.27 introduced both budget failures tracked in #248. Commit `cab5791`
placed every strong textual match before the native hits, and `01f7d4e` let
the entire evidence pass spend its budget before considering a long native
hit without an anchor. The shared Hook regression recreates the 67-candidate
case with fictional records and checks the actual short answer and long
excerpt in `additionalContext` from both Codex and CodeBuddy.

The paired Tencent Core already supplies native L1 order and score, and the
Server adapter uses its scenario list, scenario read and core profile handlers.
The scenario list can include native descriptions. The current AB results
contain verified scene bodies and provenance, while list descriptions and
the Server's exact scenario reader are internal to the adapter.

## Content-free receipts

The question Hook now saves one private `recall-receipts/<request_key>.json`
per attempt as well as the latest summary in `last-recall.json`. The request key
binds the approved scope, origin, session, turn and full question. The wire
request ID is `global_recall_<request_key>`. Schema 2 receipts contain hashes of
the session, turn, actual query, returned and selected record IDs/content, and emitted
context, plus layer/count/byte totals and the number omitted for budget. When
an exact duplicate adds no evidence, `unselected_without_evidence` counts it
separately from budget omissions.
For a selected excerpt or index, `content_sha256` identifies the verified candidate and
`output_content_sha256` identifies the answer body actually presented.
An enriched index also carries `index_metadata_sha256` for its title and
description, which the offline fallback verifies. Receipts
contain no query, memory text, source paths, credentials or exception values.
Later queries cannot overwrite a different request's evidence. Old schema 1
summaries remain readable but cannot establish request identity.

An optional recall cache or diagnostic receipt write can fail after the
verified context is ready. The Hook can still emit the context. After stdout
flushes, the CLI attempts the output deduplication stamp and independently
updates the diagnostic receipt. A stamp write failure leaves the turn eligible
for retry even when the receipt confirms the earlier output.

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

For isolated diagnosis, a Hook process started with `AGENT_BRAIN_RECALL_TIMING=1`
can write a private `last-recall-timing.json` beside `last-recall.json`. The
sampling is off by default. The file is replaced after a sampled failure or
after that Hook flushes stdout and saves its output acknowledgment. Its request
key and exact receipt SHA-256 identify the associated
receipt, including when a later attempt has the same request key. Offsets use one
monotonic origin from entry to the Client CLI main function. They cover Hook initialization,
recall and its HTTP request, credential reads, DNS, TCP, TLS, headers and body
reads, and preparing and flushing Hook output. These offsets measure work within
the Client CLI. Failure timing retains the interval
from failure to saving the normal receipts. Response header timing
keeps the read count, the first eight spans and the final span. CodeBuddy's
normal Hook has no Client request deadline, so its request budget is null. The
sample contains no response bytes, prompt text, credentials or addresses. The
timing sample is separate from the normal receipt and Hook output, and failure
to save a sample leaves the original recall outcome intact.

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
