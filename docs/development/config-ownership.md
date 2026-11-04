# Codex configuration updates

The updater owns the authenticated `mcp_servers.agent_brain_server` binding and
the tagged Agent Brain Hook commands and their invocation settings. It does not own a whole event, a whole TOML
table, or everything between its marker comments. Other tools, host trust state,
model settings and user comments stay in the user's configuration.

Both platforms use the same `tomllib` based semantic editing code. Quoted keys,
dotted keys, inline tables and ordinary tables identify the same fields. Actual
TOML statements determine boundaries, so header or marker text inside multiline
strings is data. Unrelated statements retain their bytes; a shared inline
assignment is re-encoded only when it contains a managed field being changed.
No tool-name allowlist or per-device configuration hash catalog is needed.

Global Hook refresh compares only its tagged groups against the private receipt.
Other events, groups within the same event, and foreign commands sharing a group
can change independently.
A change to the managed command, timeout or options reports its field path and
preserves the file. The updater retains capture approval, source identity,
`global.off`, host trust decisions and a paused MCP service.

The public update/check-update receipt includes `conflict_field` for a managed
conflict, including the affected MCP table when only a table digest is known.
A continuously edited file reports `config-edit-in-progress` with a retry action.
Neither receipt echoes command values or hides behind a generic Hook failure.

Before an authorized launcher update starts writing, its plan can be regenerated
against the latest unowned configuration. The package, policy, registration and
other approved inputs must stay the same. Existing applying transactions retain
their exact recovery images. Ordinary Hook writes retry a cancelled prewrite
against the latest file up to three times; persistent editing produces a busy
message. A conflicting managed edit is checked again before retry and preserved.

Whole-file transaction hashes remain for crash recovery and detecting a change
between preflight and commit. They do not define ordinary configuration
ownership. Windows keeps its existing file object and security descriptor;
POSIX keeps the staged write and metadata checks. No permission expansion,
credential access, source activation or host trust approval is added.

New Hook journals use schema 3 and reconstruct their semantic target during
recovery. Schema 2 journals retain their historical splitter and exact target
validation, including interrupted in-place writes.

Hash-only legacy migrations can recover their managed baseline from an existing
transaction image whose bytes match the migration's recorded config hash. The
live file can then contain unrelated changes and a paused service. If neither a
semantic pin nor any original image remains, the old hash cannot identify which
fields changed: restore that original postimage from its backup once and rerun
the update. The resulting modern registration pins only the service table;
re-pinning the modified live file is not a safe migration method.

These are source changes. A release and installation remain separate actions.
