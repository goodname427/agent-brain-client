# L2/L3 recall and delivery evidence

The ordinary MCP `memory_query` searches canonical destination Memory records.
It does not read Tencent's L2 scenarios or L3 profile. Those derivatives travel
through `memory_query`, the read-only MCP `memory_query` entry and the owned `UserPromptSubmit` Hook. The Hook
uses the current question, validates generation provenance and source
applicability, and emits `hookSpecificOutput.additionalContext`.

`memory_query` accepts the current origin, a distinct reader session ID, a
question and an optional limit of 1–3. It uses the existing HTTPS identity and
Server source gate, retaining the layer and committed event provenance; it does
not enable sources, replay the outbox or call a model. See the
[Windows acceptance repairs](windows-acceptance-repairs.md) for admission diagnostics
and the distinction between ordinary topic search and layered recall.

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
- The three-item limit, size bounds and reference-only applicability remain.
  Unknown fields, mismatched devices, project-only peer groups and malformed
  source context fail closed.

`tests/test_layered_recall.py` covers both path directions, valid L2/L3 output,
legacy compatibility, negative scope/provenance cases and bounded empty groups.
It runs in the full Mac suite and Windows contract jobs. The subprocess fixture
`tests/layered_recall_bridge.py` supports an explicit Server integration runner;
it is never part of a release and never activates host credentials.

## Content-free receipts

After a successful query, the Hook writes `last-recall.json` beside its private
settings. `global-status` returns it as `recall_observation`. It contains only
time, stage, status, L1/L2/L3 counts, item count, context byte count, offline cache
flag, schema, and `model_used: unknown`. It contains no query, memory text,
session ID, source path, device ID, credential or exception message. This is the
last successful preparation observation, not a claim about the current turn or
every historical attempt. A later failure leaves the previous timestamp intact.

- `unobserved`: the receipt is absent. Old timestamp-only prompt stamps cannot
  prove which layer was found.
- `empty`: the query completed with no injectable results.
- `context-prepared`: results passed validation and Hook context was prepared.
- A separate content-free stderr receipt, `hook-output-written`, is emitted
  after stdout is written and flushed. It cannot prove that the host accepted
  the output or that an Agent used it in its answer.

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

The only daily query tool is `memory_query`; the hidden `memory_recall` alias forwards to it. Automatic Hooks also send `memory_query`. Direct Agent facts and extracted facts share native L1 after the Server's reviewed migration cutover. New native save receipts carry `storage=tencent-l1` and a checkpoint digest; local persistence does not prove remote backup.
