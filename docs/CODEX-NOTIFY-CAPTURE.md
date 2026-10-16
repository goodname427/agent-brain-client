# Codex notify capture scope

`codex_notify_mux.py` preserves an existing callback's argv and can enqueue a
sanitized capture event. Capture is disabled unless the invocation explicitly
supplies `--capture-source-mode`, `--capture-cwd-allowlist-json`, and
`--capture-codex-home`:

* `--capture-cwd-allowlist-json` is a JSON array of canonical absolute project
  directories, and the event's `cwd` exactly matches one entry. A missing,
  different, aliased, or unavailable working directory skips capture.
* `--capture-codex-home` is the canonical source home. The notify process must
  have both `CODEX_HOME` and `AGENTBRAIN_NOTIFY_CAPTURE_CODEX_HOME` set to that
  exact value. The content MCP process must inherit the same binding; during
  initialization `ContentAdapter` rejects an explicit `--codex-home` override
  that differs from it.
* `--capture-source-mode synthetic` is for synthetic acceptance only. It also
  requires a marker file `.agent-brain-client-synthetic-codex-home` with exact
  content `agent-brain-client synthetic Codex home v1` followed by a newline,
  plus a real, empty `memories/` directory. The mux never creates the marker.
* `--capture-source-mode authorized` is for an explicitly authorized user
  source. It permits existing Memory entries; the normal MCP policy and grant
  still control access. The expected-home binding above must be present in both
  processes so a configured `--codex-home` cannot silently point elsewhere.
  An unknown or missing mode skips capture.

The original callback still receives the original event JSON byte-for-byte
whenever capture is skipped. These checks are runtime guardrails; they do not
configure Codex or establish that a different thread has loaded the expected
MCP. For an isolated synthetic check, configure the `content_mcp.py` entry with
the same `CODEX_HOME` and `AGENTBRAIN_NOTIFY_CAPTURE_CODEX_HOME`; if it has an
explicit `--codex-home`, it must match. Start the isolated Codex process with
the same environment, and use only an isolated synthetic project directory in
the cwd allowlist. Keep the synthetic `memories/` directory empty while the
capture thread runs.

Example argument shape (Codex appends the event JSON after these arguments):

```sh
python3 /absolute/path/adapters/codex/codex_notify_mux.py \
  --callback-argv-json '["/absolute/path/SkyComputerUseClient","turn-ended"]' \
  --capture-argv-json '["/absolute/path/python3","/absolute/path/adapters/codex/content_mcp.py","--codex-notify"]' \
  --capture-cwd-allowlist-json '["/absolute/path/to/synthetic-project"]' \
  --capture-codex-home '/absolute/path/to/synthetic-codex-home' \
  --capture-source-mode synthetic
```

The example is documentation only; it does not install or activate a notify
hook. Codex notify configuration is user-level on the supported setup, so
adding it would also affect other projects using that user profile. Do not add
this hook to a real user profile for synthetic testing. A future live setup
needs an explicitly isolated Codex profile and a separately reviewed activation
step. The repository project config is not an activation mechanism for notify.
