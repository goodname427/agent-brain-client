# Global automatic memory

The user-level entry supports local Codex workflows that deliver the official
UserPromptSubmit and Stop events. Cloud-orchestrated threads do not
run local command Hooks. Installing this entry does not prove native delivery;
new definitions use the host's normal Hook review/trust flow.

## Install/update the entry

```sh
sh tools/memory.sh global-install
sh tools/memory.sh global-status
```

Installation is repeatable and owns only a marked inline Hook block in the user
Codex config. Other Hooks, MCP tables and legacy hooks.json stay intact. Existing project definitions are preserved. After explicit unified-source approval, their future collection is stopped and handed to the shared router; their old data and outbox are retained without import or replay. Without expanded source approval the entry
returns before reading Hook stdin or accessing credentials. No new directory or
non-project conversation is collected merely because the entry was installed.

## Select sources once

An existing operator reference must be configured by the installation owner; it
reuses their existing SSH identity and verified host. It contains references,
never key material. Source setup previews a scope, then applies its exact digest.

```sh
sh tools/memory.sh global-configure \
  --include-root /absolute/personal-projects \
  --isolate-root /absolute/separate-projects \
  --exclude-root /absolute/company-work \
  --non-project-root /absolute/personal-notes
# Review the preview, then use the printed command:
sh tools/memory.sh global-configure --approve-scope PRINTED_SCOPE_SHA256
```

These are illustrative paths, not guessed exclusions. Include roots share one
personal memory namespace. Isolate roots keep repository memories separate;
those contexts can recall the personal pool plus their own project. A more
specific include/isolate rule wins; exclusions always win. Non-project contexts
are disabled unless a non-project directory is explicitly included. Worktree
exclusions check both checkout and repository location. No Git history, remote
configuration, native transcripts or chat database is read.

The preview declares future prompt/final-assistant text and necessary origin
metadata going to the existing Server and configured DeepSeek Flash. The Server
binds this exact expanded scope to the existing owner/device and cumulative
unlimited allowance. Existing usage is never reset; no billing top-up occurs.
Changing a source policy first fences collection. An unconfirmed operator result
leaves the fence in place and must be inspected before another apply.

The default is unified cross-project retrieval for approved future sources. Users do not mark each memory global. Retrieval uses the actual UserPromptSubmit question and verified source context; other-project conventions remain historical reference and never become current project rules. Explicit project-only/no-share markers exclude the turn from upload. No historical memory is imported or silently promoted.

For an owner who explicitly approves all local future conversations with no directory exclusions, configure `--include-root / --non-project-root /`. Directory approval is separate from per-memory relevance or applicability.

## Use/control

```sh
sh tools/memory.sh global-status
sh tools/memory.sh global-recall
sh tools/memory.sh global-sync
sh tools/memory.sh global-stop
sh tools/memory.sh global-resume
sh tools/memory.sh global-uninstall
```

Run recall from an approved context. It returns at most three provenance-checked
derived items and no L0. Recognized transport failures may return scope-bound
cached items marked offline/unconfirmed. Sync retries the original admitted
outbox operations, rechecking source exclusions and LocalOnly/recognized secret
patterns; it never invents a new model request ID to bypass uncertainty.

Global stop also requests stop for the registered legacy source. Resume preserves
markers that had already been stopped individually. Uninstall removes only the
owned user-level Hook block and retains data, queues and other project entries.

Directory exclusions apply before cache/upload/injection; previously excluded
origin data is filtered from later derived recall. LocalOnly markers and known
credential patterns provide an additional content gate, not a classifier for all
business confidential information. Company locations must be explicitly selected
for exclusion or left outside approved personal roots.
