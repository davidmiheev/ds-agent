# ds-agent — personal Claude Code server with BYOK + MCP

A single FastAPI process that hosts Claude Code in a browser, plugs in your own
LLM keys (OpenRouter, MiniMax, Anthropic, custom), and runs MCP servers
(Colab MCP etc.) per session. Single-user, single-host, single-process.

## What it does

- **Web UI** at `http://localhost:8765` with a chat pane, terminal pane
  (xterm.js for shell output), session sidebar, and workspace file browser.
  No build step — Jinja templates + HTMX + Alpine.js + xterm.js via CDN.
- **BYOK** — paste your OpenRouter / MiniMax / Anthropic key in the Settings
  page. Stored encrypted at `~/.coding-agent/keys.enc` (Fernet, key derived
  from `APP_PASSWORD`) or plaintext `keys.json` if no password.
- **Configurable MCPs** — edit `~/.coding-agent/mcp.json` in the UI; the
  server composes a per-session `.mcp.json` and injects provider env vars
  (`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY=""`).
- **Deliverables inline** — when the agent runs a script and emits a marker
  like `__ARTIFACT__:plot:/path/to/plot.png`, the server reads the file,
  base64-encodes it, and the browser renders it as a `<img>`. CSV / JSON /
  markdown become downloadable links. Same for any image format.
- **Chat with follow-ups** — `ClaudeSDKClient` keeps the subprocess warm
  across turns, so the second message in a session has the full prior
  context. Page reload resumes the session from the saved transcript.
- **Interrupt** — `stop` button sends `{type: "interrupt"}` over the WS;
  the SDK cancels the in-flight turn.

## MCPs (Colab, filesystem, research, anything)

`~/.coding-agent/mcp.json` is the global registry. The server composes a
per-session `.mcp.json` (with placeholders substituted) and writes it into
the session workspace before spawning the Claude CLI subprocess. Each MCP
stdio server is then a child of the claude CLI — fully isolated to the
session.

**Placeholders** (resolved at session start, so `mcp.json` stays
host-agnostic and can be committed / shared):

- `${ROOT}` — the project checkout root (where `src/` and `mcp.json` live)
- `${DATA_DIR}` — `~/.coding-agent` (or `$CODING_AGENT_HOME`)
- `${VAULT:provider_key}` — a BYOK key from the encrypted key store

**Shipped MCPs (all three verified end-to-end):**

- **filesystem** — `npx -y @modelcontextprotocol/server-filesystem <path>`
- **colab** — custom `src/colab_mcp/colab_server.py` wrapper around
  `googlecolab/google-colab-cli`. Programmatic Colab access — no browser
  required. See "Colab MCP" section below.
- **research** — custom `src/research_mcp/server.py`, 12 tools for academic
  / bio / quant search. See "Research MCP" section below.
- **ds** — custom `src/ds_mcp/server.py`, runs code in a dedicated
  data-science Python env (pandas/sklearn/plotting). See "Data-science
  environment" section below.
- **kaggle** — Kaggle's official remote MCP (`https://www.kaggle.com/mcp`)
  via `mcp-remote`, token auth. See "Kaggle MCP" section below.

### Research MCP (academic / bio / quant search)

`src/research_mcp/server.py` is a single stdio MCP server with 12 tools that
hit free, mostly no-auth-required APIs:

| Tool | What it searches |
|------|------------------|
| `arxiv_search` | arXiv papers (field qualifiers: `au:`, `ti:`, `abs:`) |
| `semantic_scholar_search` | Cross-domain academic with citations |
| `openalex_search` | Open scholarly works catalog (no rate limit) |
| `pubmed_search` | PubMed biomedical literature (NCBI E-utilities) |
| `crossref_lookup` | DOI → full metadata |
| `biorxiv_search` | bioRxiv preprints |
| `hf_search_models` | HuggingFace models (set `HF_TOKEN` env for higher rate) |
| `hf_search_datasets` | HuggingFace datasets |
| `uniprot_search` | UniProt proteins (Swiss-Prot + TrEMBL) |
| `pdb_search` | RCSB PDB protein structures |
| `ensembl_search` | Ensembl gene lookup (default: human) |
| `fred_series` | FRED economic time-series (requires `FRED_API_KEY` env) |

**Auth model:**
- Most tools need no auth.
- `hf_*` work without a token (rate-limited); for higher rate limits set `HF_TOKEN` in the mcp.json env block.
- `fred_series` needs `FRED_API_KEY` (free at https://fred.stlouisfed.org/docs/api/api_key.html) — the tool returns a clear error if it's missing.

**SSL:** the server reads `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` from its environment (set by the user / mcp.json) and uses that as the CA file. On a normal dev box, the system default is used. On sandboxed hosts (e.g. behind a corporate proxy), point `REQUESTS_CA_BUNDLE` at your CA bundle path.

### Colab MCP (programmatic, no browser)

`src/colab_mcp/` is a small stdio MCP server I wrote that wraps the official
`googlecolab/google-colab-cli` library. It gives the agent the full
programmatic Colab API:

| Tool | What it does |
|------|--------------|
| `colab_auth` | Start or complete the OAuth flow. |
| `colab_new`  | Provision a runtime (CPU / T4 / L4 / A100 / TPU). |
| `colab_status` | Show active session info. |
| `colab_execute` | Run Python code, get stdout + image outputs. |
| `colab_install` | Install Python packages on the runtime. |
| `colab_sessions` | List active runtimes on the account. |
| `colab_stop` | Release the runtime. |

**Auth is much easier than colab-mcp's proxy mode.** The wrapper reuses
the public OAuth client that ships inside `google-colab-cli` — you don't
have to register your own GCP OAuth client. On first use, the agent
calls `colab_auth` with no arguments, gets back an auth URL, you open
it in any browser, sign in to your Google account, paste the code
back via `colab_auth(code=...)`, and you're done. Tokens are cached
at `~/.config/colab-cli/token.json` and reused.

**No vault keys required for the new wrapper** — it pulls the OAuth
client config from inside the google-colab-cli package itself.

**Setup:** `bash src/colab_mcp/setup.sh` creates a venv at
`src/colab_mcp/.venv/` and installs the dependencies (google-colab-cli,
jupyter-kernel-client, mcp, etc.) directly from
`https://github.com/googlecolab/google-colab-cli`. Then point mcp.json
at the venv's python.

```json
{
  "mcpServers": {
    "colab": {
      "command": "/abs/path/ds-agent/src/colab_mcp/.venv/bin/python",
      "args": ["-m", "colab_mcp.colab_server"],
      "timeout": 120000,
      "env": {
        "PYTHONPATH": "/abs/path/ds-agent/src"
      }
    }
  }
}
```

The `PYTHONPATH` is required so the spawned process can import
`colab_mcp.colab_server` (the claude CLI launches the MCP subprocess
with `cwd` set to the per-session workspace, not the project root).

**Per-session override:** the mcp config accepts a session-level
`mcp_overrides` JSON so you can disable or swap servers per session
without editing the global file.

### Data-science environment (dedicated Python + ds MCP)

The agent does data work in a **dedicated Python environment** at
`~/.coding-agent/ds-env/` (override with `CODING_AGENT_HOME`), separate
from the server's own venv. Install it on any Linux box:

```bash
bash scripts/install_ds_env.sh
# optional: DS_PY_VERSION=3.11 bash scripts/install_ds_env.sh
# optional: DS_EXTRA_PACKAGES="xgboost torch" bash scripts/install_ds_env.sh
```

Preinstalled: pandas, numpy, scipy, scikit-learn, statsmodels, matplotlib,
seaborn, plotly, polars, pyarrow, openpyxl, kaggle CLI, requests, tqdm.
The script is idempotent — re-run to upgrade.

The **`ds` MCP** (`src/ds_mcp/server.py`) exposes that env to the agent:

| Tool | What it does |
|------|--------------|
| `ds_env` | Interpreter path + versions of the key packages. |
| `ds_run` | Run inline code or a script in the DS env (default 600s timeout). |
| `ds_preview` | Load a csv/parquet/xlsx/json → shape, dtypes, nulls, head, describe. |
| `ds_install` | pip-install extra packages into the DS env. |

`DS_PYTHON` is injected into every session's env (see `providers.py`), so
the agent can also just run `$DS_PYTHON script.py` in the shell. The system
prompt (`agent_prompt.py`) tells the agent to prefer `ds_preview` on new
datasets and never use bare `python3` for data work.

**Dataset upload:** the composer has a 📎 button that POSTs a file to
`/v1/sessions/{sid}/upload` (csv/tsv/parquet/xlsx/json/jsonl/feather/h5/pkl/
npy/npz, 2 GB cap). Files land in `<workspace>/data/` and the UI auto-sends
the agent a nudge so it previews the dataset.

### Kaggle MCP (official remote server)

Kaggle ships an official remote MCP at `https://www.kaggle.com/mcp`
(see https://www.kaggle.com/docs/mcp) with tools for notebooks,
competitions, datasets, models, and benchmarks. We bridge it with
`mcp-remote` (stdio → HTTP) and authenticate with a Kaggle API token:

```json
"kaggle": {
  "command": "npx",
  "args": ["-y", "mcp-remote", "https://www.kaggle.com/mcp",
           "--header", "Authorization: Bearer ${VAULT:kaggle}"],
  "timeout": 60000
}
```

**Setup:**
1. Get a token: kaggle.com → Settings → *Create New Token* (starts with
   `KGAT…`).
2. In the UI: **Settings → BYOK keys → provider `kaggle`** → paste the
   token. It's stored in the encrypted key vault as provider `kaggle`.
3. The `${VAULT:kaggle}` placeholder in mcp.json is resolved at session
   start, so the token never appears in the per-session `.mcp.json`
   plaintext beyond the spawned process's env.

Requires `npx` (Node.js) on the host.

## Token economy (built-in)

- **Model picker** — click "+ new session" to get a dropdown of models.
  Curated set per provider (Anthropic, OpenAI, Google, DeepSeek, Meta, Qwen)
  with a "smartest / fastest / cheap / reasoning" tag. OpenRouter's live
  catalog is merged in at request time.
- **Prompt caching** — the SDK auto-injects `cache_control` markers for
  Anthropic models. OpenRouter forwards them. The chat shows the live
  `cache_read_tokens` and `cache_hit_pct` after each turn, so you can
  see how much you're saving.
- **Per-turn usage + cost** — after every turn, the chat pane appends a
  `done — $0.0123 · 4,521 in / 287 out · 3,801 cached read · cache 84%`
  line.
- **Context window bar** — the header shows `ctx 14%` and grows towards
  red. Click it to manually compact. The Claude Agent SDK also has
  **auto-compact** built in (`isAutoCompactEnabled: true` is the default)
  — when the context fills up, the SDK summarizes older turns
  automatically.
- **Tool-result trimming** — `TOOL_RESULT_MAX_BYTES` (default 30KB) caps
  how much of a tool's output is sent back to the model. Larger outputs
  are saved to `<workspace>/.truncated/<tool>-<hash>.txt` and the model
  sees a head + "truncated" + tail with a pointer to the full file.

## Quick start

```bash
# 1. Install (uv)
cd ds-agent
uv sync

# 2. Configure .env (gitignored — create it yourself)
echo 'OPENROUTER_API_KEY=sk-or-...' > .env
echo 'APP_PASSWORD=some-strong-password' >> .env   # skip for no-password mode

# 3. (optional) dedicated data-science env for the agent (pandas/sklearn/…)
bash scripts/install_ds_env.sh

# 4. Run (port 8765, loads .env, sets PYTHONPATH=src/, auto-frees a busy port)
bash scripts/run_server.sh
```

Open `http://localhost:8765`, log in, then **+ new session** in the sidebar.
If you set `OPENROUTER_API_KEY` in `.env` it's auto-seeded as the OpenRouter
BYOK key at startup; otherwise paste your key in **Settings → BYOK keys**.

(Manual equivalent: `PYTHONPATH=src uv run uvicorn ds_agent.app:app
--host 127.0.0.1 --port 8765`.)

## Run on a public host

Same code. One extra env var + Caddy for auto-TLS:

```bash
# on the VPS, with DNS pointing at it
echo 'APP_PUBLIC=1' >> .env
echo "APP_PASSWORD=$(openssl rand -base64 32)" >> .env

# install Caddy
sudo apt install -y caddy
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy

bash scripts/run_server.sh   # listens on 127.0.0.1:8765
# open https://coding.yourdomain.com
```

See `deploy/Caddyfile` — it's a 5-line reverse proxy with auto-TLS.

## How BYOK reaches the model

Claude Code's CLI honors three env vars to redirect from first-party Anthropic
to any Anthropic-compatible endpoint. The session manager picks the right
combination per provider (`app/providers.py`):

```python
# OpenRouter
{
  "ANTHROPIC_BASE_URL":   "https://openrouter.ai/api",
  "ANTHROPIC_AUTH_TOKEN": "<your-openrouter-key>",
  "ANTHROPIC_API_KEY":    "",   # MUST be empty, not unset
  "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
}
```

The `ANTHROPIC_API_KEY=""` (empty string, not unset) is critical — otherwise
the CLI silently falls back to first-party auth and you get 401s.

## Artifact convention

When the agent wants the user to see a file (plot, CSV, report, …), it
prints a single line on its own in the tool's stdout:

```
__ARTIFACT__:<kind>:/absolute/path
```

Recognized kinds: `plot`, `png`, `jpg`, `jpeg`, `svg`, `pdf`, `csv`,
`json`, `text`, `md`, `html`, `ipynb`. The server reads the file, base64-
encodes it, and emits a `<div class="artifact" data-kind="…"
data-mime="…" data-name="…" data-b64="…">` element in the streamed
tool result. The browser's `expandArtifacts()` (in `static/app.js`)
replaces it with an inline `<img>` (for images) or a download link.

Example for matplotlib:

```python
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
plt.plot([1,2,3,4])
plt.savefig('/workspace/plot.png', bbox_inches='tight')
print('__ARTIFACT__:plot:/workspace/plot.png')
```

The default system prompt (see `agent_prompt.py`) explains this to the
agent. You can override it by editing the file.

## Tests

`tests/test_ds2.py` is a real end-to-end test: asks the agent to make a plot,
verifies that the tool_use / tool_result / artifact HTML all come through
the WebSocket. Run with the server up (tests expect port 8765):

```bash
bash scripts/run_server.sh &   # or in another shell
SID=$(curl -s -X POST localhost:8765/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{"provider":"openrouter","model":"anthropic/claude-sonnet-4-5","title":"plot test"}' \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
uv run python tests/test_ds2.py "$SID"
```

## File layout

```
ds-agent/
├── src/
│   ├── ds_agent/             # the web server package
│   │   ├── app.py            # FastAPI: routes, WS bridge
│   │   ├── core.py           # paths, env
│   │   ├── db.py             # SQLite (sessions, cookies, usage)
│   │   ├── crypto.py         # Fernet for BYOK keys
│   │   ├── providers.py      # BYOK env per provider
│   │   ├── sessions.py       # SDK subprocess lifecycle + message serialization
│   │   ├── artifact_parser.py# __ARTIFACT__: → inline <img>/download
│   │   ├── trim.py           # tool-result trimmer
│   │   ├── agent_prompt.py   # default append-system-prompt (data science)
│   │   ├── model_catalog.py  # curated model list + live OpenRouter enrichment
│   │   ├── templates/        # Jinja
│   │   └── static/           # app.js, style.css
│   ├── colab_mcp/            # custom Colab MCP wrapper
│   │   ├── colab_server.py
│   │   └── setup.sh
│   ├── research_mcp/         # custom research/quant/bio search MCP
│   │   └── server.py
│   └── ds_mcp/               # data-science env MCP (ds_run/ds_preview/…)
│       └── server.py
├── tests/                    # end-to-end smoke tests + transcripts
├── docs/                     # arch.md, todo.md, debug_notes.md
├── scripts/
│   ├── run_server.sh         # start server on port 8765 (auto-frees busy port)
│   └── install_ds_env.sh     # create ~/.coding-agent/ds-env (pandas/sklearn/…)
├── deploy/Caddyfile          # reverse proxy for the public-host case
└── mcp.json                  # global MCP registry (copy is in ~/.coding-agent)
```

Data lives at `~/.coding-agent/` (override with `CODING_AGENT_HOME`):
- `state.db` — sessions, cookies
- `mcp.json` — your MCP config
- `keys.enc` (or `keys.json` if no `APP_PASSWORD`)
- `sessions/<id>/` — per-session workspace + `.mcp.json` + `.claude/`
- `workspaces/<id>/` — files the agent can read/write
- `workspaces/<id>/data/` — uploaded datasets
- `ds-env/` — dedicated data-science Python env (from install_ds_env.sh)
