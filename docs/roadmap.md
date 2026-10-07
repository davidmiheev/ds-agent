# Roadmap: prioritized work queue

Snapshot as of 2026-09-29 (extended 2026-10-07 with observability, Modal and
DeepInfra), built from a review of the code, `docs/*`, and the
GitHub PR list. `docs/todo.md` remains the running checklist; this is the
ranked view across it, the plan docs, and problems found in the review.
Priority reflects impact on safety and correctness first, then agent
capability, then polish.

## In flight

| PR | State | Needs |
|---|---|---|
| #4 Vast.ai MCP Phase 1 | Open, merged up to date with `main`, tests pass | Human review, then a funded-account smoke test of `vast_new` (a live rental bills real money, so it was never run) |
| #1, #2, #3, #5, #6 | Merged | Nothing |

## P0: safety and correctness

1. **Gate deploys on tests.** `.github/workflows/deploy.yml` deploys on every
   push to `main` and has no test step, so a broken merge goes live. Add a job
   running the no-credentials test scripts (`test_search_export_memory`,
   `test_agent_mcp_tools`, `test_kaggle_mcp_proxy`, `test_vast_mcp_*`,
   `test_colab_mcp_reconnect`) before the deploy job, and make deploy depend
   on it. Small change, large risk reduction.
2. **`can_use_tool` gating for `APP_PUBLIC=1`.** `sessions._spawn_client` always
   uses `permission_mode="bypassPermissions"` while `core.py` promises
   hardened approvals in public mode. Either implement it or stop claiming
   it. Blocks exposing the server beyond localhost or a trusted VPS.
3. **Workspace secrets are browsable.** `GET /v1/sessions/{sid}/files` lists
   and `/files/raw` serves everything under the workspace, including
   `.mcp.json` and `.claude/settings.local.json`, which hold resolved API
   keys (search and export already exclude them via `core.WORKSPACE_SECRET_*`).
   Apply the same exclusions in both routes.
4. **Path check uses a string prefix.** `read_file` guards traversal with
   `str(full).startswith(str(ws_root))`; use `Path.is_relative_to`. Not
   exploitable today (workspace ids are fixed-length) but it is the wrong
   idiom on a file-serving endpoint.
5. **Vast.ai live verification** (see In flight). Until then the highest-cost
   tool in the repo has only been tested against mocked HTTP.

## P1: agent capability and reliability

6. **Fail fast on models the bundled CLI can't use.** Downgraded from a
   confirmed hang: `google/gemma-4-31b-it` worked after the SDK/CLI upgrade
   (`docs/debug_notes.md`, update dated 2026-09-06), but any newer model can
   still hang a first turn for the full 5-minute watchdog. A cheap
   session-creation smoke query with a short timeout would report it in
   seconds.
7. **Observability.** Today the only signals are unstructured stderr logs
   (read via `journalctl`), a `/healthz` that returns `{"ok": true}` without
   checking anything, and `session_usage`, which keeps only the *last* turn's
   usage per session. Several past incidents were found by hand: a dead MCP
   child that stayed dead for the life of its CLI process, hung model calls,
   and Telegram sends lost to transient network errors. Proposed, in order:
   1. Structured (JSON) logs with `session_id`, `turn_id`, tool name and
      duration on every tool call and watchdog/respawn event.
   2. A real `/healthz`: DB reachable, Telegram poller alive, and per-session
      MCP child liveness (the exact gap behind the dead-`memory`/`kaggle`
      incidents).
   3. Usage history, not just last turn: an append-only `turn_usage` table so
      cost per session, per model and per day can be queried and shown in the
      UI.
   4. Alerts to the existing Telegram bot on watchdog fires, respawns, MCP
      child death, and budget-cap hits.
   5. Optional: OpenTelemetry traces or a Prometheus `/metrics` endpoint, only
      if a collector actually exists; the first four need no new infrastructure.
8. **Market data MCP** (`yfinance` or Polygon/Alpaca/Tiingo). Biggest
   capability gap for the quant use case; FRED and econometrics already exist.
9. **SEC EDGAR filings and transcripts** in `research_mcp`.
10. **Vast.ai idle-timeout watchdog** (plan Phase 3). Warn or auto-`vast_stop`
    after N idle hours; never auto-destroy without confirmation. Do this
    before trusting long unattended runs.
11. **Modal as a third compute backend** (serverless GPU, alongside Colab and
    Vast.ai). Not yet researched: before writing code, read the real SDK and
    write a `docs/modal-mcp-plan.md` in the same style as the Vast.ai plan.
    Open questions to settle there: Modal auth is a token id plus a secret,
    two values, while the BYOK vault stores one key per provider; execution
    is function or sandbox based rather than a long-lived instance, so the
    cost model differs (per-second billing, no idle instance to forget, but
    function timeouts and cold starts); and whether the SDK's dependencies
    conflict with this project's pins, as `vastai` did with `cryptography`.
12. **DeepInfra as an LLM provider.** `providers.py` already has a generic
    gateway path (`ANTHROPIC_BASE_URL` plus an auth token), but the Claude
    CLI speaks the Anthropic Messages API. Spike first: confirm whether
    DeepInfra exposes an Anthropic-compatible endpoint. If it only offers
    OpenAI-compatible endpoints, this needs a translation proxy, which is a
    larger change than a new provider entry. Either way it also needs a
    `model_catalog` source (live catalog or a curated list) and a check that
    its models handle tool calls well enough for the agent loop.
13. **Turn correlation for overlapping web + Telegram turns.** Known remaining
    limitation in `docs/debug_notes.md` (a subscriber can misattribute the
    tail of another interface's in-flight turn). Narrow window, but it is a
    correctness bug in the shared engine.
14. **Quant artifact templates**: system-prompt guidance for Plotly candlestick,
    drawdown, and return-heatmap charts and `quantstats` tearsheets.

## P2: engineering hygiene

15. **Turn the ad-hoc test scripts into pytest** with a server fixture on a random
    port. Prerequisite for item 1 being pleasant; the live-server tests
    (`test_ws`, `test_colab_status`, ...) hardcode `127.0.0.1:8765` and need real
    model spend, so split them from the credential-free ones.
16. **Canonical Colab venv in tests.** `test_colab_mcp_server.py` prefers
    `.venv-313`, an empty venv; use `src/colab_mcp/.venv` first.
17. **Implement or remove `SESSION_BACKEND=docker`.** Declared in `core.py`,
    only `local` works.
18. **Repo consistency checks in CI**: `mcp.json` valid JSON, `deploy_remote.sh`
    passes `bash -n`, README MCP count matches `mcp.json`. Each has already
    drifted once.

## P3: scale and polish (gated on real usage, per the plan docs)

19. **Search: Tier 1 tokenized matching** (`docs/search-improvements-plan.md`),
    reusing the `/models` fix in `telegram.py`. Then FTS5 only once transcripts
    are large enough to notice.
20. **Memory: Phase 1 typed/supersedable nodes** (`docs/memory-graph-plan.md`).
    Do it once real memories exist to look at; the table was empty when the
    plan was written.
21. **Vast.ai Phase 2**: SSH/SCP for large files, only if the base64 upload
    proves too slow for a real dataset.
22. **Rate limiting and audit log for `APP_PUBLIC`**, and a UI guard for two
    concurrent websockets on one session.
23. **Move `tests/*.txt` transcripts** under `tests/transcripts/`.

## Doc drift fixed in this pass

- README and `docs/todo.md` described an embedded xterm.js terminal. The
  xterm assets are loaded in `base.html` but no terminal panel exists in
  `index.html` or `app.js`, so the claims were removed.
- `docs/todo.md`'s "unrecognized models hang" item was stale (see item 6).
