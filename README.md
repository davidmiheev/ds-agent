# 📊 ds-agent — Agent for Data Science Tasks with Kaggle & Google Colab MCP

> A personal, single-process AI data science & quantitative research agent server featuring Claude Code / Claude Agent SDK in the browser, BYOK (Bring Your Own Keys), inline plot/dataset artifact rendering, and built-in MCPs for **Kaggle**, **Google Colab**, **Data Science Execution**, **Quantitative Research**, and **Academic Literature**.

---

## 🌟 Overview

**ds-agent** is an autonomous assistant built specifically for end-to-end data science and quantitative research workflows: exploratory data analysis (EDA), statistical & econometric modeling, strategy backtesting, machine learning benchmarking, scientific literature retrieval, and cloud compute execution.

### Key Capabilities
- **Official Kaggle MCP**: Search, query, and download datasets, competition data, models, and benchmarks directly via Kaggle's official remote MCP gateway.
- **Programmatic Google Colab MCP**: Provision cloud runtimes (CPU, T4, L4, A100, TPU), execute heavy training/inference scripts, install remote dependencies, and capture plots—no browser automation needed.
- **Dedicated Data Science Environment & MCP (`ds_mcp`)**: Fast, isolated data science environment (`pandas`, `polars`, `numpy`, `scikit-learn`, `scipy`, `statsmodels`, `seaborn`, `plotly`, `pyarrow`, etc.) with one-call dataset inspection (`ds_preview`) and code execution (`ds_run`).
- **Quantitative & Macroeconomic Research**: Integrated Federal Reserve Economic Data (FRED) API series query, statistical/econometric modeling (`statsmodels`, `scipy`), financial backtesting guidance (`vectorbt`, `backtesting.py`), and quantitative finance paper search (`q-fin` on arXiv).
- **Academic & Bio/Quant MCP (`research_mcp`)**: 12 search tools across arXiv, Semantic Scholar, OpenAlex, PubMed, bioRxiv, Hugging Face (models & datasets), UniProt, PDB, Ensembl, and FRED economic time-series.
- **Inline Artifact Rendering**: Matplotlib figures, Seaborn charts, and Plotly visualizations render inline immediately. CSVs, Parquet files, JSON data, and Markdown reports surface as one-click downloads.
- **Token Economy & Prompt Caching**: Real-time cost calculation, cache hit tracking (`cache_read_tokens`), automated context compaction, and output trimming.
- **Zero-Build Web UI**: Clean, responsive interface featuring streaming chat, live tool inspection, an embedded xterm.js terminal, dataset uploader (up to 2 GB), and workspace file browser.

---

## 🏗️ Architecture

```
                                  ┌──────────────────────────────┐
                                  │      Web Browser UI          │
                                  │ (HTMX + Alpine.js + xterm.js)│
                                  └──────────────┬───────────────┘
                                                 │
                                                 │ WebSocket / HTTP (:8765)
                                                 ▼
┌─────────────────────────┐       ┌──────────────────────────────┐
│       BYOK Vault        │──────►│    FastAPI Agent Server      │
│ (Fernet Encrypted Keys) │       │   (src/ds_agent/app.py)      │
└─────────────────────────┘       └──────────────┬───────────────┘
                                                 │
                                                 ▼
                                  ┌──────────────────────────────┐
                                  │   Claude Agent SDK Client    │
                                  │  (Warm Subprocess per Turn)  │
                                  └───┬──────────┬───────────┬───┘
                                      │          │           │
                 ┌────────────────────┘          │           └────────────────────┐
                 ▼                               ▼                                ▼
  ┌─────────────────────────────┐  ┌───────────────────────────┐   ┌─────────────────────────────┐
  │         Kaggle MCP          │  │     Google Colab MCP      │   │      DS Environment MCP     │
  │   (Official Remote MCP)     │  │ (google-colab-cli runtime)│   │  (Dedicated Python Sandbox) │
  │  • Datasets & Competitions  │  │  • T4 / L4 / A100 / TPU   │   │  • ds_preview & ds_run      │
  │  • Models & Benchmarks      │  │  • Remote Python execute  │   │  • EDA, Pandas, Scikit-Learn│
  └─────────────────────────────┘  └───────────────────────────┘   └─────────────────────────────┘
```

---

## 🧩 Built-in MCP Suite

Each session receives an isolated `.mcp.json` composed dynamically from the global registry (`mcp.json` / `~/.coding-agent/mcp.json`).

### 1. Kaggle MCP (Official Remote Server)
Connects directly to Kaggle's official remote MCP at `https://www.kaggle.com/mcp` via `mcp-remote`, giving the agent access to Kaggle's data catalog, competitions, and models:
- Search and download competition files and test sets.
- Query community datasets and pull metadata or CSVs.
- Explore model weights and benchmark leaderboards.

**Setup**:
1. Generate an API token on [Kaggle](https://www.kaggle.com) (Settings → *Create New Token*, starts with `KGAT…`).
2. Enter the token in the UI (**Settings → BYOK keys → provider `kaggle`**).
3. The token is securely injected via `${VAULT:kaggle}` without plaintext leakage.

### 2. Google Colab MCP (Programmatic GPU/TPU Runtimes)
Powered by `src/colab_mcp/colab_server.py` wrapping `google-colab-cli`, enabling the agent to manage and run code on Google Colab hardware without opening a browser:

| Tool | Action |
|------|--------|
| `colab_auth` | Run or finish the OAuth authentication handshake. |
| `colab_new` | Spin up a fresh runtime (`CPU`, `T4`, `L4`, `A100`, `TPU`). |
| `colab_status` | Query active compute session state and resource usage. |
| `colab_execute` | Execute Python code remotely; returns stdout and image outputs. |
| `colab_install` | Install packages via pip on the remote Colab instance. |
| `colab_sessions` | List all active runtime instances associated with the account. |
| `colab_stop` | Terminate and release the remote runtime. |

**Auth Flow**:
Google Colab uses PKCE OAuth (`code_verifier`). To avoid PKCE state mismatches or IP mismatch across remote redirects, the recommended flow is:

1. **Authorize locally once** (on your development machine):
   ```bash
   bash src/colab_mcp/setup.sh
   src/colab_mcp/.venv/bin/python src/colab_mcp/auth_once.py
   ```
   Open the printed URL, log in with your Google account, and paste the code back into your terminal. The token will be saved to `~/.config/colab-cli/token.json`.

2. **Auto-synced to remote**:
   When you run `bash scripts/deploy_remote.sh`, your local `~/.config/colab-cli/token.json` is automatically copied to `/home/agent/.config/colab-cli/token.json` on the remote server with secure permissions (`600`).

3. **Manual copy to existing remote (if needed)**:
   ```bash
   scp ~/.config/colab-cli/token.json root@<REMOTE_IP>:/home/agent/.config/colab-cli/token.json
   ssh root@<REMOTE_IP> "chown agent:agent /home/agent/.config/colab-cli/token.json && chmod 600 /home/agent/.config/colab-cli/token.json"
   ```
   The token auto-refreshes indefinitely.

### 3. Dedicated Data Science Environment & MCP (`ds_mcp`)
Isolates analytical workloads in a dedicated Python environment located at `~/.coding-agent/ds-env/` (pre-populated with top scientific computing libraries):

| Tool | Action |
|------|--------|
| `ds_preview` | **Automated EDA**: Inspects CSV, Parquet, Excel, Feather, or JSON files. Returns row/col dimensions, column types, null counts, sample head rows, and distribution summaries. |
| `ds_run` | Runs Python code or analytical scripts in the DS environment (with configurable timeouts up to 600s). |
| `ds_env` | Returns the Python interpreter path and installed library versions. |
| `ds_install` | Installs additional libraries (`xgboost`, `lightgbm`, `torch`, `vectorbt`, `yfinance`, etc.) into the DS environment. |

### 4. Academic & Bio/Quant Research MCP (`research_mcp`)
A unified search server with 12 tools for literature and biological/economic data:
- **Macroeconomics & Quantitative Data**: `fred_series` (Federal Reserve Economic Data for interest rates, inflation, GDP, yield curves, monetary indicators).
- **Literature**: `arxiv_search` (supporting CS, stats, `q-bio`, and `q-fin`), `semantic_scholar_search`, `openalex_search`, `pubmed_search`, `biorxiv_search`, `crossref_lookup`.
- **Machine Learning & Data**: `hf_search_models`, `hf_search_datasets`.
- **Bioinformatics**: `uniprot_search`, `pdb_search`, `ensembl_search`.

### 5. Telegram Bot Integration
Interact directly with the agent from Telegram on mobile or desktop. It runs
as a background long-polling worker (`src/ds_agent/telegram.py`) alongside
the FastAPI server whenever a bot token is configured — no separate process
or public webhook needed.

**Setup**:
1. Create a bot with [@BotFather](https://t.me/BotFather) and grab its token.
2. Add to `.env`:
   ```bash
   TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"
   # Optional whitelist of Telegram user IDs (comma-separated); if unset, anyone can use the bot
   TELEGRAM_ALLOWED_USERS="12345678,87654321"
   ```
3. Start the server (`bash scripts/run_server.sh`) and message your bot on Telegram — an unauthorized user gets their numeric ID back in the denial message, handy for populating `TELEGRAM_ALLOWED_USERS`.

**Commands**:

| Command | Action |
|---|---|
| `/start`, `/help` | Welcome message and usage instructions |
| `/models [query]` | List/filter live or curated model IDs as tappable buttons for `/new` |
| `/new [model]` | Start a fresh session (defaults to `claude-sonnet-4-5` via OpenRouter, or the first configured BYOK provider) |
| `/sessions` | List the 10 most recent sessions, marking the active one |
| `/switch <id>` | Switch the chat to a different session (prefix match on session ID) |
| `/compact` | Compact the active session's context |
| `/stop` | Interrupt the agent mid-turn |
| `/status` | Show the active session's ID, title, model, provider, and workspace path |

**Behavior**:
- Each Telegram chat maps to one ds-agent session at a time (`/new` creates one automatically on first message if none exists), with a per-chat lock so turns don't overlap.
- Assistant replies stream as edited messages; thinking blocks and intermediate tool narration are filtered out so only the end result is sent, with Markdown converted to Telegram-safe HTML (code blocks preserved).
- Generated plots/artifacts are automatically attached as photos or documents.
- Send a file (CSV, JSON, Parquet, `.py`, etc.) directly in chat to upload it into the active session's workspace — the agent is prompted to inspect it automatically.

---

## 📈 Quantitative Research & Financial Analysis

ds-agent is optimized for quantitative analysis, econometric modeling, and algorithmic research:

1. **Macroeconomic Indicators (FRED API)**:
   - Call `fred_series(series_id="DGS10")` (10-Year Treasury Yield), `fred_series(series_id="CPIAUCSL")` (Consumer Price Index), `fred_series(series_id="FEDFUNDS")` (Federal Funds Effective Rate), etc.
   - Set `FRED_API_KEY` in environment or UI settings for full access.

2. **Time-Series & Econometrics**:
   - `statsmodels` & `scipy` pre-installed for ARIMA, GARCH, vector autoregression (VAR), cointegration tests, and OLS factor regressions.
   - High-performance data manipulation via `pandas`, `polars`, and `pyarrow`.

3. **Strategy Backtesting & Quantitative Modeling**:
   - Agent prompts are tuned for backtesting workflows (`vectorbt`, `backtesting.py`).
   - Seamless installation of market data toolkits (`yfinance`, `pandas-ta`, `quantstats`) via `ds_install` or `DS_EXTRA_PACKAGES`.

4. **Financial Visualizations**:
   - Generate interactive Plotly or Matplotlib equity curves, drawdown charts, correlation matrices, and candlestick charts, instantly surfaced in the chat UI via `__ARTIFACT__:plot:...`.

---

## 🖼️ Artifact Convention & Inline Visualization

When executing data science scripts, the agent surfaces visual plots, tables, and reports directly in the chat stream using stdout markers:

```
__ARTIFACT__:<kind>:/absolute/path
```

### Supported Artifact Kinds
- **Visualizations** (`plot`, `png`, `jpg`, `jpeg`, `svg`): Rendered directly as interactive image elements in the chat.
- **Datasets & Tables** (`csv`, `json`, `text`, `md`, `html`, `ipynb`): Displayed with instant download links and preview cards.
- **Documents** (`pdf`): Rendered with inline preview links.

#### Example: Matplotlib / Seaborn Output
```python
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns

df = sns.load_dataset('penguins')
sns.scatterplot(data=df, x='flipper_length_mm', y='body_mass_g', hue='species')
plt.title('Penguin Morphology Analysis')
plt.savefig('/workspace/penguin_morphology.png', bbox_inches='tight')
print('__ARTIFACT__:plot:/workspace/penguin_morphology.png')
```

---

## ⚡ Quick Start

### 1. Prerequisites
- **Python 3.11+**
- **[uv](https://github.com/astral-sh/uv)** (fast Python package manager)
- **Node.js (npx)** (for Kaggle and filesystem MCP bridges)

### 2. Installation
```bash
# Clone the repository
git clone git@github.com:davidmiheev/ds-agent.git
cd ds-agent

# Sync project dependencies
uv sync

# Set up the dedicated data-science environment (pandas, scikit-learn, etc.)
bash scripts/install_ds_env.sh

# Optional: include extra quant / ML libraries
DS_EXTRA_PACKAGES="yfinance vectorbt pandas-ta xgboost torch" bash scripts/install_ds_env.sh
```

### 3. Environment Configuration
Create a `.env` file in the project root:
```bash
# Optional default provider API key
OPENROUTER_API_KEY=sk-or-v1-...

# Optional Web UI login password (leave unset for passwordless local mode)
APP_PASSWORD=your-secure-password

# Optional FRED API key for economic data (free at https://fred.stlouisfed.org)
FRED_API_KEY=your-fred-api-key
```

### 4. Run the Agent Server
```bash
bash scripts/run_server.sh
```

Open `http://localhost:8765` in your browser, create a new session, and start exploring datasets or training models.

---

## 🔑 Bring Your Own Keys (BYOK)

ds-agent supports direct API keys from multiple LLM providers:
- **OpenRouter**
- **Anthropic**
- **MiniMax**
- **Custom Anthropic-compatible endpoints**

Keys are stored locally with Fernet encryption in `~/.coding-agent/keys.enc` (derived from `APP_PASSWORD`) or `~/.coding-agent/keys.json`.

---

## 📁 Repository Layout

```
ds-agent/
├── src/
│   ├── ds_agent/             # Core FastAPI web server & agent controller
│   │   ├── app.py            # FastAPI routing, WebSocket streaming bridge
│   │   ├── core.py           # Paths, home directory, environment resolution
│   │   ├── db.py             # SQLite database for sessions, tokens, and cookies
│   │   ├── crypto.py         # Fernet encryption for BYOK key vault
│   │   ├── providers.py      # Environment variables & gateway configs per provider
│   │   ├── sessions.py       # Claude Agent SDK subprocess lifecycle & message bus
│   │   ├── artifact_parser.py# Parser for __ARTIFACT__: markers -> inline HTML
│   │   ├── trim.py           # Intelligent tool-result output trimmer
│   │   ├── agent_prompt.py   # Data science & quant system prompt & artifact guidelines
│   │   ├── model_catalog.py  # Model registry & live OpenRouter catalog fetcher
│   │   ├── templates/        # Jinja2 HTML templates
│   │   └── static/           # Client-side JavaScript (HTMX, Alpine, xterm, artifacts)
│   │
│   ├── colab_mcp/            # Google Colab MCP server
│   │   ├── colab_server.py   # stdio MCP server wrapping google-colab-cli
│   │   └── setup.sh          # Colab environment setup script
│   │
│   ├── ds_mcp/               # Data Science execution MCP
│   │   └── server.py         # ds_preview, ds_run, ds_env, ds_install tools
│   │
│   └── research_mcp/         # Academic literature & scientific/economic data MCP
│       └── server.py         # arXiv (CS/q-fin), PubMed, FRED, OpenAlex, HuggingFace
│
├── deploy/
│   └── Caddyfile             # 5-line Caddy reverse proxy configuration with automatic TLS
├── docs/                     # Architecture notes (arch.md), roadmap (todo.md), CI/CD docs (ci-cd.md)
├── scripts/
│   ├── run_server.sh         # Startup script on port 8765 (with auto port-reclaim)
│   ├── install_ds_env.sh     # Installer for dedicated ~/.coding-agent/ds-env
│   └── deploy_remote.sh      # One-command remote VPS deploy (app + MCPs + systemd)
├── tests/                    # End-to-end WebSocket tests & MCP verification
├── mcp.json                  # Global MCP registry configuration
└── pyproject.toml            # Project dependencies and packaging definition
```

---

## 🧪 Testing

Run end-to-end integration tests (requires server running on port 8765):

```bash
# Start server in background
bash scripts/run_server.sh &

# Create a test session and verify plot generation & WebSocket events
SID=$(curl -s -X POST http://localhost:8765/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{"provider":"openrouter","model":"anthropic/claude-sonnet-4-5","title":"test"}' \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

uv run python tests/test_ds2.py "$SID"
```

---

## 🚀 One-Command Remote Deployment

Deploy a fully working agent (app + all 5 MCP servers + systemd service) to any
Ubuntu/Debian VPS with a single command:

```bash
# from the project root (a .env with your keys must exist)
bash scripts/deploy_remote.sh <REMOTE_IP> <PATH_TO_SSH_KEY>

# e.g.
bash scripts/deploy_remote.sh 203.0.113.10 ~/.ssh/id_ed25519_1
```

Both arguments are optional — the script asks for them interactively if omitted.
Optional env overrides: `REMOTE_USER` (default `root`), `REMOTE_PORT` (default `22`),
`REMOTE_DIR` (default `/opt/coding-agent`), `APP_PORT` (default `8765`).

The script:
1. Copies the project (rsync) and `.env` (chmod 600) to the host.
2. Installs Node.js (for the `filesystem` + `kaggle` MCPs) if missing.
3. Creates a non-root `agent` user — the agent CLI refuses
   `--dangerously-skip-permissions` under root, so the service must not run as root.
4. Installs `uv`, builds the app venv (`uv sync`), the Colab MCP venv, and the
   data-science env (`ds-env`).
5. Installs the live MCP config to `~/.coding-agent/mcp.json`.
6. Installs and starts the `coding-agent` systemd service (auto-start on boot,
   restart on failure).
7. Health-checks `/healthz` and verifies every MCP server with an initialize handshake.

Then open `http://<REMOTE_IP>:8765` and log in with your `APP_PASSWORD`.

Useful on the remote:
```bash
systemctl status coding-agent      # service state
journalctl -u coding-agent -f      # live logs
```

### CI/CD: GitHub Actions Deploy Workflow

`.github/workflows/deploy.yml` runs `scripts/deploy_remote.sh` automatically
from GitHub Actions (push to `main`, or manually via `workflow_dispatch`).
See **[docs/ci-cd.md](docs/ci-cd.md)** for the required repository secrets
and how it's configured.

### Manual / HTTPS Deployment

To host `ds-agent` on a public VPS with HTTPS via Caddy:

```bash
# Set public mode and a strong password in .env
echo 'APP_PUBLIC=1' >> .env
echo "APP_PASSWORD=$(openssl rand -base64 32)" >> .env

# Install Caddy reverse proxy
sudo apt update && sudo apt install -y caddy
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy

# Launch agent server
bash scripts/run_server.sh
```

> ⚠️ `APP_PUBLIC=1` sets `Secure` cookies, so it only works behind TLS (e.g. Caddy
> with a real domain). On a raw IP without TLS keep `APP_PUBLIC=0`.
