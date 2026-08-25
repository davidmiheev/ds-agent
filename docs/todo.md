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

## Open

### High priority
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
- [ ] Session export (transcript → markdown)
- [ ] Multi-session concurrency guard UI (currently second WS just fails)
- [ ] Rate limiting / audit log for `APP_PUBLIC` mode
- [ ] Move `tests/*.txt` transcripts under `tests/transcripts/`
