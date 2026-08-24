# Architecture

Personal Claude Code server with BYOK + MCP. A single FastAPI process that
hosts Claude Code in a browser, plugs in your own LLM keys (OpenRouter,
MiniMax, Anthropic, custom gateways), and runs MCP servers (Colab, research,
filesystem, anything) per session. Single-user, single-host, single-process.

## Repository layout

```
ds-agent/
├── src/
│   ├── ds_agent/              # the web server (FastAPI app package)
│   │   ├── app.py             # FastAPI entrypoint: UI pages, REST, WebSocket bridge
│   │   ├── core.py            # config, paths, env loading (DATA_DIR, APP_PASSWORD, ...)
│   │   ├── db.py              # SQLite store (sessions, auth cookies, usage)
│   │   ├── crypto.py          # BYOK key storage (Fernet-encrypted, password-derived)
│   │   ├── sessions.py        # session lifecycle + ClaudeSDKClient subprocess mgmt
│   │   ├── providers.py       # BYOK key → env vars for the claude CLI subprocess
│   │   ├── model_catalog.py   # curated model picker + live OpenRouter catalog
│   │   ├── agent_prompt.py    # appended system prompt (data-science guidance)
│   │   ├── artifact_parser.py # __ARTIFACT__ marker → inline images/files
│   │   ├── trim.py            # tool-result trimming (head/tail + pointer file)
│   │   ├── static/            # app.js, style.css (no build step, CDN libs)
│   │   └── templates/         # Jinja: base/index/login/settings
│   ├── colab_mcp/             # stdio MCP server wrapping google-colab-cli
│   │   ├── colab_server.py    # 7 tools: auth/new/status/execute/install/sessions/stop
│   │   └── setup.sh           # creates colab_mcp/.venv with google-colab-cli deps
│   └── research_mcp/          # stdio MCP server, 12 academic/bio/quant search tools
│       └── server.py
├── tests/                     # end-to-end WS/HTTP test scripts + transcripts
├── docs/                      # this folder
├── scripts/run_server.sh      # start server on port 8765 with all envs
├── mcp.json                   # global MCP registry (copied to ~/.coding-agent/)
└── pyproject.toml             # uv-managed deps
```

## Runtime topology

```
Browser (HTMX + Alpine.js + xterm.js via CDN)
   │  HTTP (pages, /v1/* REST) + WebSocket /ws/sessions/{sid}
   ▼
FastAPI app (src/ds_agent/app.py)  ── SQLite (~/.coding-agent/state.db)
   │  one ClaudeSDKClient per active session (kept warm across turns)
   ▼
claude CLI subprocess (cwd = per-session workspace)
   │  reads workspace/.mcp.json + .claude/settings.local.json
   ▼
MCP stdio subprocesses (children of the claude CLI, isolated per session)
   ├── filesystem  (npx @modelcontextprotocol/server-filesystem)
   ├── colab       (src/colab_mcp/colab_server.py in its own venv)
   └── research    (src/research_mcp/server.py)
```

## Key flows

### Session spawn (`sessions.py::_spawn`)
1. Load the stored BYOK key for the session's provider (`crypto.load_key`).
2. Build provider env vars (`providers.env_for`) — e.g. OpenRouter sets
   `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, and `ANTHROPIC_API_KEY=""`
   (empty string, not unset — otherwise the CLI falls back to first-party auth).
3. Render the session workspace: compose `.mcp.json` from the global registry
   (`~/.coding-agent/mcp.json`) merged with per-session `mcp_overrides`,
   resolving `${VAULT:provider_key}` placeholders against the encrypted key
   store; write `.claude/settings.local.json` with the provider env.
4. Spawn `ClaudeSDKClient` with `permission_mode="bypassPermissions"`,
   `setting_sources=["project"]`, the appended system prompt, and `resume`
   when a prior transcript exists.

### History persistence across tab refreshes
- The chat pane is rebuilt from the **on-disk SDK transcript** on every
  `open()`: `GET /v1/sessions/{sid}/history` → `sessions.load_history()`
  reads the newest `~/.claude/projects/<slugified-cwd>/<uuid>.jsonl`
  (slug = cwd with every non-alphanumeric char → `-`), maps entries to UI
  messages (user / assistant / thinking / tool / tool-result), and runs
  tool-result text through the artifact rewriter so `__ARTIFACT__` plots
  re-embed after a refresh. Last-turn usage comes from the DB.
- The active session id survives reloads via the **URL hash** (`#<sid>`,
  shareable) with a `sessionStorage` fallback; `init()` picks
  hash → sessionStorage → first sidebar entry.

### WebSocket bridge (`app.py::ws_session`)
- One WS per session at a time; the SDK client is shared (`in_use` flag).
- Inbound frames: `user` (queued → `client.query()`), `interrupt`, `ping`.
- Outbound: every SDK message serialized to JSON (`_serialize` handles
  dataclass content blocks + injected `type` discriminators). Tool results
  pass through artifact rewriting (`artifact_parser`) and trimming (`trim`)
  before being forwarded.

### BYOK key storage (`crypto.py`)
- Fernet encryption with a key derived from `APP_PASSWORD`
  (`sha256(password)` → urlsafe-b64). Stored at `~/.coding-agent/keys.enc`.
- No password → plaintext `keys.json` (chmod 600), localhost-only mode.

### Deliverables / artifacts (`artifact_parser.py`)
- Agent scripts emit `__ARTIFACT__:kind:/path` markers; the server reads the
  file, base64-encodes images (rendered inline as `<img>`), and turns
  CSV/JSON/markdown into download links.

### Token economy
- Per-turn usage + cost recorded in `session_usage` (cache read/creation
  tokens, hit %, cost USD) from the SDK's `ResultMessage.model_usage`.
- Context window bar via `client.get_context_usage()`; manual `/compact`
  plus SDK auto-compact.
- Tool-result trimming: outputs over `TOOL_RESULT_MAX_BYTES` (30 KB default)
  are saved to `<workspace>/.truncated/` and replaced with head + tail.

## Data layout (`~/.coding-agent/`, override with `CODING_AGENT_HOME`)

```
state.db                 SQLite: sessions, auth_cookies, session_usage
keys.enc / keys.json     BYOK keys (encrypted / plaintext fallback)
mcp.json                 global MCP registry
sessions/<sid>/          transcripts (transcript.jsonl)
workspaces/<sid>/        per-session workspace (claude CLI cwd, .mcp.json)
```

## Environment variables

| Var | Default | Purpose |
|-----|---------|---------|
| `OPENROUTER_API_KEY` | — | auto-seeded as the `openrouter` BYOK key at startup (only if none stored yet) |
| `APP_PASSWORD` | `""` | single-user login; empty → no-password mode |
| `APP_PUBLIC` | `0` | `1` → Secure cookies, password required, hardened tool approvals |
| `SESSION_BACKEND` | `local` | `local` subprocess (docker backend planned) |
| `MAX_BUDGET_USD` | `5.0` | hard per-session spend cap |
| `CODING_AGENT_HOME` | `~/.coding-agent` | data dir |
| `TOOL_RESULT_MAX_BYTES` / `TOOL_RESULT_KEEP_HEAD` / `TOOL_RESULT_KEEP_TAIL` | 30K/8K/4K | trimming knobs |
| `HF_TOKEN`, `FRED_API_KEY`, `REQUESTS_CA_BUNDLE` | — | research MCP options |

## MCP auth models

- **colab**: reuses the public OAuth client shipped inside
  `google-colab-cli` — no GCP OAuth registration, no vault keys. Tokens
  cached at `~/.config/colab-cli/token.json`. Needs its own venv
  (`bash src/colab_mcp/setup.sh`) because google-colab-cli pins Python 3.13
  deps.
- **research**: mostly no-auth public APIs; `HF_TOKEN` / `FRED_API_KEY`
  optional via the mcp.json env block.
- **vault placeholders**: any mcp.json value may use `${VAULT:key}` which is
  substituted from the encrypted key store at session-render time.
