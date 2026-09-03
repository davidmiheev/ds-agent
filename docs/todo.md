# TODO

Current state as of 2026-08-25, right after the repo restructure
(tests → `tests/`, sources → `src/`, docs → `docs/`, `scripts/run_server.sh`).

## Done ✅

- FastAPI web UI (chat + terminal + session sidebar + file browser), no build step
- BYOK key storage (Fernet-encrypted via `APP_PASSWORD`, plaintext fallback)
- Provider env injection (Anthropic / OpenRouter / generic gateway)
- Per-session `.mcp.json` composition with `${VAULT:...}` resolution
- filesystem MCP (npx), colab MCP (custom wrapper, OAuth via google-colab-cli's
  public client), research MCP (12 tools) — all three verified end-to-end
  (transcripts in `tests/*_end_to_end_test.txt`)
- Artifact inlining (`__ARTIFACT__` markers → images / download links)
- Tool-result trimming with `.truncated/` pointer files
- Per-turn usage/cost tracking + cache stats, context bar, manual + auto compact
- Session resume from saved transcripts; interrupt button
- Repo restructure: `src/` layout, `tests/`, `docs/`, `scripts/run_server.sh` (port 8765)
- History persistence across tab refresh: `GET /v1/sessions/{sid}/history`
  rebuilds the chat from the on-disk SDK transcript (artifacts re-embedded);
  active session survives reload via URL hash + sessionStorage. Also fixed
  the broken `resume=` wiring (was passing our sid / checking a legacy
  transcript path — now uses the SDK session UUID from the transcript).
- Quantitative research foundations: `fred_series` in `research_mcp` for macroeconomic
  time-series, `statsmodels`/`scipy`/`plotly` in `ds-env`, arXiv `q-fin` search support,
  and `vectorbt`/`backtesting` system prompt guidance.
- Quant package presets in `install_ds_env.sh` (`INSTALL_QUANT=1` / `DS_PRESET=quant`):
  `yfinance`, `pandas-ta`, `vectorbt`, `backtesting`, `arch`, `quantstats`.
- Kaggle MCP verified end-to-end (71 tools; `search_datasets` returns live data).
  Token auto-seeded from `KAGGLE_API_TOKEN` in `.env` → BYOK "kaggle" provider.
- Chat UI: live streaming now renders text + tool calls in correct interleaved order
  (was: all text first, then all tool calls); code blocks get syntax highlighting
  (highlight.js) + a copy button.
- File links: text-based files served as `text/plain` + `Content-Disposition: inline`
  so they don't blank-tab / force-download (images/PDFs unchanged).
- Stuck-agent watchdog: `stream_events` is now long-lived with an inactivity
  timeout (`TURN_INACTIVITY_TIMEOUT`, default 300s) → interrupt → respawn if the
  CLI died. UI clears `busy` on error/watchdog frames. Root cause documented in
  `docs/debug_notes.md` (hung OpenRouter model call, not an MCP tool).
- `__ARTIFACT__` markers in assistant text (not just tool stdout) are now
  rewritten server-side and rendered as links/images in the chat (live + history).
- Colab MCP production hardening: `colab_server.py` + `research_mcp/server.py`
  strip inherited `*_PROXY` env vars at startup so Google/academic API calls
  don't hit the dead `socks5://127.0.0.1:1080` proxy (InvalidSchema: no socksio).
  Verified end-to-end through the live server (`colab_status` → "No active session").
- `research_mcp` migrated from the MCP 1.x decorator API to the 2.x API
  (`MCPServer` + `@server.tool`) to match the main `.venv`'s mcp 2.x; tool names
  unchanged. (colab_mcp stays on 1.x in its own venv, pinned `mcp[cli]<2`.)
- Colab one-time OAuth helper: `src/colab_mcp/auth_once.py` (standalone, reuses
  colab-cli's public OAuth client); token at `~/.config/colab-cli/token.json`.
- Cross-session memory & search MCP (`src/ds_agent/agent_mcp.py`, 6th MCP
  server, wired into `mcp.json`): `list_sessions`, `search_other_sessions`,
  `get_session_summary` let the agent find and reuse prior work across
  sessions; `remember`/`recall`/`forget` are a persistent memory notebook
  (new `memories` table in `db.py`) whose recent entries are injected into
  every session's system prompt (`agent_prompt.build_append_system_prompt`).
  Search/export both explicitly exclude `.mcp.json` / `.claude/settings.local.json`
  (resolved secrets — see `core.WORKSPACE_SECRET_*`) — covered by
  `tests/test_search_export_memory.py`'s leak assertions.
- Session export (`src/ds_agent/export.py`): messages → markdown +
  a zip of the session's workspace artifacts, capped per-file/total size.
  Wired into the web UI (chat header "export" button →
  `GET /v1/sessions/{sid}/export`) and Telegram (`/export`, ~45 MB cap for
  Telegram's upload limit).
- Fixed `sessions.get_active()` — `telegram.py`'s `/stop` command called it
  but it never existed, so `/stop` always crashed with `AttributeError`.

## Open

### High priority
- [ ] **Models the bundled CLI doesn't recognize hang instead of failing
      fast** (`/new <model>` in Telegram, or a raw id typed in the web
      picker) — e.g. `google/gemma-4-31b-it`, a real, current OpenRouter
      model (verified against the live catalog) that's simply newer than
      the pinned `claude-agent-sdk`/CLI's internal model list — logs
      `[claude-code:unrecognized_model]` and then hangs the first turn for
      the full `TURN_INACTIVITY_TIMEOUT` (5 min) before the watchdog
      recovers. See `docs/debug_notes.md` 2026-09-03. Validating against
      OpenRouter's live catalog won't catch this (the id is genuinely
      valid there) — needs either an SDK/CLI version bump or a
      session-creation-time smoke-test query with a short timeout so a
      CLI-incompatible id is caught and reported in seconds.
- [x] `deploy/Caddyfile` was referenced in README but missing — recreated
      during the restructure (reverse proxy → 127.0.0.1:8765).
- [ ] **`SESSION_BACKEND=docker` is declared in `core.py` but not implemented** —
      only `local` (bare subprocess) works. Either implement per-session
      containers or remove the env var.
- [ ] **`can_use_tool` gating for public mode** — comments in `core.py` /
      `sessions.py` promise hardened tool approvals when `APP_PUBLIC=1`, but
      `_spawn` always uses `permission_mode="bypassPermissions"`. Must be fixed
      before exposing on a public host.
- [x] **`mcp.json` contained sandbox-specific paths** (`/workspace/coding-agent`,
      `/home/david/...`) — now host-agnostic via `${ROOT}` / `${DATA_DIR}`
      placeholders resolved at session-render time (see `sessions.py`
      `_resolve`). Only the sandbox CA path (`/etc/ssl/certs/agent-identity/...`)
      remains host-specific, which is expected.

### Medium
- [ ] Tests in `tests/` are ad-hoc scripts (plain `asyncio.run(main())`), not
      pytest — convert to pytest with a fixture that boots the server on a
      random port; they currently hardcode `127.0.0.1:8765`.
- [ ] `test_colab_mcp_server.py` prefers `.venv-313/bin/python`, which is an
      empty venv on this machine — decide on the canonical colab venv
      (`src/colab_mcp/.venv` via `setup.sh`) and update the fallback order.
- [x] README quick start updated to `bash scripts/run_server.sh`
      (module path `ds_agent.app:app`, port 8765).
- [x] `mcp.json.example` updated to the new `colab_mcp.colab_server` wrapper
      and now uses `${ROOT}` / `${DATA_DIR}` placeholders (host-agnostic).
- [x] `.gitignore` added (`.venv*`, `__pycache__`, `.env`).

### Quantitative Research & Financial Data Roadmap 📈
- [ ] **Market Data MCP / Tools**: Add Yahoo Finance (`yfinance`) or Polygon/Alpaca/Tiingo integration for instant OHLCV historical asset prices and real-time quotes.
- [ ] **SEC EDGAR & Financial Filing Search**: Add 10-K / 10-Q filing retrieval and earnings transcript extraction tool to `research_mcp`.
- [x] **Quant Package Presets in `install_ds_env.sh`**: Pre-install or document quant packages (`yfinance`, `pandas-ta`, `vectorbt`, `backtesting`, `arch`, `quantstats`).
- [ ] **Quant Artifact Templates**: System prompt guidance for generating interactive Plotly financial charts (candlesticks, drawdown curves, return heatmaps) and tearsheets (`quantstats`).

### Low / ideas
- [x] Session export (transcript → markdown + artifacts zip; web UI + Telegram `/export`)
- [ ] Multi-session concurrency guard UI (currently second WS just fails)
- [ ] Rate limiting / audit log for `APP_PUBLIC` mode
- [ ] Move `tests/*.txt` transcripts under `tests/transcripts/`
- [ ] `search_sessions()` re-reads and re-parses every session's transcript
      on every call (no index/cache) — fine at personal-tool scale, would
      need real indexing (e.g. SQLite FTS5 over a synced messages table) if
      session count/transcript size grows a lot.
- [ ] Memories have no per-session/per-project scoping or expiry — everything
      saved via `remember()` is global and injected into every session
      forever (capped at the 30 most recent). Consider tags-based filtering
      of what gets auto-injected, or a max-age/pin mechanism, if the list
      grows large enough to crowd the system prompt.
