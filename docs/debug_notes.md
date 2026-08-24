# Debug notes

Hard-won lessons from getting this stack working. Consult before re-debugging
the same symptoms.

## BYOK / provider env

- **`ANTHROPIC_API_KEY` must be set to the empty string `""` — not unset —**
  when redirecting the claude CLI to a non-Anthropic base URL (OpenRouter,
  gateways). If it's merely absent, the CLI silently falls back to
  first-party Anthropic auth and every request 401s. See `providers.py`.
- OpenRouter needs `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` for the
  live model catalog to work.

## MCP wiring

- **The claude CLI spawns MCP subprocesses with `cwd` = the per-session
  workspace, not the project root.** So `python -m colab_mcp.colab_server`
  fails with ModuleNotFoundError unless the mcp.json env block sets
  `PYTHONPATH` to the project's `src/` dir. This bit us once; every custom
  MCP entry in mcp.json needs it.
- **Colab proxy mode doesn't work headless.** The original colab-mcp
  `open_colab_browser_connection` tool returned `{"result": false}` in the
  wiring test (`tests/colab_mcp_wiring_test.txt`) — it needs a real browser
  session. That's why `src/colab_mcp/colab_server.py` was written as a
  programmatic wrapper around `googlecolab/google-colab-cli` instead.
- **Colab OAuth**: reuse the public OAuth client shipped inside
  google-colab-cli (client id `764086051850-...`). No need to register a GCP
  OAuth client; tokens cache at `~/.config/colab-cli/token.json`.
- **Colab deps need their own venv** (`bash src/colab_mcp/setup.sh` →
  `src/colab_mcp/.venv`, Python 3.13). google-colab-cli's dependency set
  conflicts with the main app venv. Note: the repo's `.venv-313/` at the
  project root is an **empty husk** (only pyvenv.cfg) — don't point anything
  at it; `tests/test_colab_mcp_server.py` falls back to `src/colab_mcp/.venv`.
- **SSL on sandboxed hosts**: behind a corporate proxy, MCP HTTP calls fail
  cert verification. Fix by setting `REQUESTS_CA_BUNDLE` (and/or
  `CURL_CA_BUNDLE`) in the mcp.json env block to the gateway CA path, e.g.
  `/etc/ssl/certs/agent-identity/sandbox-gateway-ca.crt`. `research_mcp`
  reads these explicitly (`server.py` `_ssl_ctx()`).
- `WaitForMcpServers` reports `ready: false` on the first call right after
  session start — normal; the agent should call it again. In transcripts,
  `filesystem` (npx cold start) is often "still connecting" while `colab` /
  `research` are already up.

## SDK message serialization (`sessions.py::_serialize`)

- `ResultMessage` has **no `type` field** — inject it from the class name.
- Content blocks (`TextBlock`, `ToolUseBlock`, `ThinkingBlock`,
  `ToolResultBlock`) are **dataclasses, not Pydantic models** — no
  `model_dump()`. Use `dataclasses.asdict` recursively and inject a `type`
  discriminator so the browser can switch on block kind.
- Cost/usage: sum `ResultMessage.model_usage` per-model entries; the
  top-level `total_cost_usd` sometimes differs — take `max()` of both.

## Artifacts & trimming

- Artifact markers (`__ARTIFACT__:kind:/path`) are extracted from tool
  result text **after** trimming — the parser looks at head/tail lines, so
  it still works on trimmed output. Keep that order.
- Trimmed outputs land in `<workspace>/.truncated/<tool>-<hash>.txt`; the
  model sees head + `[truncated]` + tail + a pointer to the full file.

## End-to-end test status (2026-08-23/24)

- `tests/colab_mcp_end_to_end_test.txt` — PASS: `colab_status` ("No active
  session"), `colab_auth` returns auth URL, stdio smoke lists 7 tools.
- `tests/research_mcp_end_to_end_test.txt` — `pubmed_search` returned real
  results, but the run ended in **TIMEOUT** waiting for the final `result`
  frame (the agent kept going past the 85s recv deadline). Not a server bug;
  raise the deadline or make the prompt stricter.
- `tests/colab_mcp_wiring_test.txt` — historical record of the failed
  browser-proxy approach (see MCP wiring above).
- All WS tests hardcode `http://127.0.0.1:8765` — start the server with
  `bash scripts/run_server.sh` first, and make sure an OpenRouter key is
  stored (Settings → BYOK) or session creation 400s with "no key stored".

## History / transcript

- **Transcript location**: the SDK does NOT write to
  `~/.coding-agent/sessions/<sid>/transcript.jsonl` (that path in
  `_has_transcript` is legacy — the dir is always empty). Real transcripts
  live at `~/.claude/projects/<slug>/<uuid>.jsonl` where slug = the session
  workspace path with **every non-alphanumeric char replaced by `-`**
  (so `~/.coding-agent/workspaces/abc` → `-home-david--coding-agent-workspaces-abc`
  — note the double dash from `/.`). One file per CLI process; newest mtime
  = current conversation.
- Transcript entries: `user` (string content = real user message; list
  content = tool results), `assistant` (content blocks: text/thinking/
  tool_use), plus noise types (`queue-operation`, `attachment`,
  `last-prompt`) that must be skipped. There are **no `result` entries** on
  disk — per-turn usage only exists in the DB (`session_usage`).
- `load_history` runs in a thread (`asyncio.to_thread`) — transcripts can
  be multi-MB.
- **Resume bug (fixed)**: `open()` passed our sid as `resume=`, but the SDK
  expects *its own* session UUID (the transcript filename). Also
  `_has_transcript` checked a legacy path that never exists, so resume was
  silently never set. Now `resume=_sdk_session_id(workspace)` = newest
  transcript's stem.

## Git / network

- **SSH to GitHub fails over IPv6** on this box: `git push` dies with
  `Connection closed by 64:ff9b::8c52:7903 port 22` (the IPv6 route is
  black-holed). Fix: force IPv4 —
  `git -c core.sshCommand="ssh -4" push -u github main`.
- The dead SOCKS proxy (`ALL_PROXY=socks5://127.0.0.1:1080`) also breaks
  plain `curl` — use `curl --noproxy '*'`.

## Server startup gotchas

- `app.py` resolves `static/` and `templates/` relative to `__file__`
  (`HERE = Path(__file__).parent`), so they must stay inside the
  `ds_agent` package dir — they were moved there in the restructure.
- Run via module path now: `uvicorn ds_agent.app:app` (with `src/` on
  `PYTHONPATH` or the package installed), not `app:app`.
- Empty `APP_PASSWORD` = auth fully bypassed (localhost mode). `db.check_cookie`
  returns True for any token in that case — intentional.
