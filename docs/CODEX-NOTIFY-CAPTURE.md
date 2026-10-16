# Codex notify capture scope

`codex_notify_mux.py` preserves an existing callback's argv and can enqueue a
sanitized capture event. Capture is disabled unless the invocation explicitly
supplies all of these conditions:

* `--capture-cwd-allowlist-json` is a JSON array of canonical absolute project
  directories, and the event's `cwd` exactly matches one entry. A missing,
  different, aliased, or unavailable working directory skips capture.
* `--capture-codex-home` is the canonical isolated Codex home, the process
  `CODEX_HOME` has the same exact value, and the home contains the synthetic
  marker file `.agent-brain-client-synthetic-codex-home` with the exact content
  `agent-brain-client synthetic Codex home v1` followed by a newline.
* That home has a real `memories/` directory with no entries. A marker mismatch,
  missing directory, symlink, non-empty source, or inaccessible path skips
  capture. The mux never creates the marker or changes the source.

The original callback still receives the original event JSON byte-for-byte
whenever capture is skipped. These checks are runtime guardrails; they do not
configure Codex or establish that a different thread has loaded the expected
MCP. For an isolated synthetic check, configure the `content_mcp.py` entry for
that isolated Codex home with `--codex-home` set to the same path (or omit the
override and use the matching `CODEX_HOME`). Start the isolated Codex process
with that `CODEX_HOME`, and use only an isolated synthetic project directory in
the cwd allowlist. Keep the synthetic `memories/` directory empty while the
capture thread runs.

Example argument shape (Codex appends the event JSON after these arguments):

```sh
python3 /absolute/path/adapters/codex/codex_notify_mux.py \
  --callback-argv-json '["/absolute/path/SkyComputerUseClient","turn-ended"]' \
  --capture-argv-json '["/absolute/path/python3","/absolute/path/adapters/codex/content_mcp.py","--codex-notify"]' \
  --capture-cwd-allowlist-json '["/absolute/path/to/synthetic-project"]' \
  --capture-codex-home '/absolute/path/to/synthetic-codex-home'
```

The example is documentation only; it does not install or activate a notify
hook. Codex notify configuration is user-level on the supported setup, so
adding it would also affect other projects using that user profile. Do not add
this hook to a real user profile for synthetic testing. A future live setup
needs an explicitly isolated Codex profile and a separately reviewed activation
step. The repository project config is not an activation mechanism for notify.
