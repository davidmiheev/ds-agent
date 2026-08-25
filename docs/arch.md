# Architecture & System Design

> Personal AI Data Science & Quantitative Research Agent server with BYOK (Bring Your Own Keys) and Model Context Protocol (MCP) integrations. Single-user, single-host, high-performance architecture hosting the Claude Agent SDK in the browser.

---

## 🏗️ 1. High-Level System Architecture

The following diagram illustrates the end-to-end topology from the browser client to the FastAPI server, Claude Agent SDK subprocess, MCP servers, and cloud compute runtimes:

```mermaid
flowchart TD
    subgraph Client["Browser Frontend (Zero Build Step)"]
        UI["Web UI (Jinja2 Templates)"]
        HTMX["HTMX + Alpine.js (State & Reactive DOM)"]
        XTerm["xterm.js (Terminal Log Stream)"]
        ArtExp["Artifact Expander (Inline Plots & Downloads)"]
    end

    subgraph Server["FastAPI Backend (src/ds_agent/)"]
        App["app.py (FastAPI App & WebSocket Bridge)"]
        DB[("SQLite: state.db\nSessions, Cookies, Usage")]
        Vault["crypto.py (Fernet Encrypted Key Store)"]
        Sessions["sessions.py (Session Lifecycle Manager)"]
        Parser["artifact_parser.py (__ARTIFACT__ Parser)"]
        Trimmer["trim.py (Tool-Result Output Trimmer)"]
    end

    subgraph Subprocess["Warm Claude SDK Subprocess (Per-Session Workspace)"]
        SDK["ClaudeSDKClient (Python SDK)"]
        CLI["Claude Code CLI Subprocess"]
    end

    subgraph MCPs["MCP Suite (Child Subprocesses & Remote Bridges)"]
        KaggleMCP["Kaggle MCP (Remote Gateway)\nhttps://www.kaggle.com/mcp\nCompetitions, Datasets, Models"]
        ColabMCP["Google Colab MCP (colab_server.py)\nOAuth Token Cache\nCPU / T4 / L4 / A100 / TPU"]
        DSMCP["Data Science MCP (ds_mcp/server.py)\nds_preview, ds_run, ds_env, ds_install\nDedicated ds-env Python Sandbox"]
        ResearchMCP["Research & Quant MCP (research_mcp/server.py)\narXiv, FRED macro series, PubMed,\nOpenAlex, HuggingFace, UniProt, PDB"]
        FSMCP["Filesystem MCP (npx)\nWorkspace Directory File Access"]
    end

    subgraph External["External Cloud Compute & Data Providers"]
        KaggleAPI["Kaggle API Services"]
        ColabCloud["Google Colab Cloud Hardware"]
        FREDAPI["Federal Reserve Economic Data (FRED)"]
        arXivAPI["arXiv / PubMed / Semantic Scholar"]
        LLMGateways["LLM Endpoints\n(OpenRouter / Anthropic / MiniMax)"]
    end

    UI --> App
    HTMX -->|HTTP REST / Uploads| App
    XTerm -->|WebSocket /ws/sessions/sid| App
    App --> DB
    App --> Vault
    App --> Sessions
    Sessions -->|Injected Provider Env & Config| SDK
    SDK --> CLI
    CLI --> KaggleMCP
    CLI --> ColabMCP
    CLI --> DSMCP
    CLI --> ResearchMCP
    CLI --> FSMCP

    KaggleMCP --> KaggleAPI
    ColabMCP --> ColabCloud
    ResearchMCP --> FREDAPI
    ResearchMCP --> arXivAPI
    SDK -->|Chat Completions / Streaming| LLMGateways

    CLI -.->|Raw Tool Stdout| Trimmer
    Trimmer -.-> Parser
    Parser -.->|Structured JSON + Artifacts| App
    App -.->|WebSocket Frames| ArtExp
```

---

## 🔄 2. End-to-End Session Execution Flow

When a user initiates a prompt in the chat, the system orchestrates key resolution, workspace rendering, tool invocation, and real-time artifact streaming:

```mermaid
sequenceDiagram
    autonumber
    actor User as User (Browser)
    participant WS as WebSocket Bridge (app.py)
    participant SM as Session Manager (sessions.py)
    participant Vault as Crypto Vault (crypto.py)
    participant SDK as ClaudeSDKClient
    participant MCP as MCP Tool Subprocess
    participant LLM as LLM Provider Gateway

    User->>WS: Send message {type: "user", text: "..."}
    WS->>SM: Ensure session subprocess warm
    alt Subprocess not running / new turn
        SM->>Vault: Load provider key (Fernet decrypted)
        Vault-->>SM: Raw API Key
        SM->>SM: Compose workspace/.mcp.json & .claude/settings.local.json
        SM->>SDK: Spawn warm client with provider env & system prompt
    end
    WS->>SDK: Query(user_message)
    SDK->>LLM: Stream chat completions (with prompt caching)
    LLM-->>SDK: Model emits Tool Use (e.g. ds_run / colab_execute)
    SDK->>MCP: Call tool via stdio MCP protocol
    MCP-->>SDK: Tool stdout (e.g. analysis + "__ARTIFACT__:plot:/workspace/fig.png")
    SDK-->>SM: Stream tool_result message
    SM->>SM: Trim excessive output (trim.py)
    SM->>SM: Extract __ARTIFACT__ markers & base64-encode files (artifact_parser.py)
    SM->>WS: Forward serialized WebSocket frame
    WS->>User: Stream text, tool results & render inline <img> / download links
    SDK->>LLM: Stream tool_result back to model
    LLM-->>SDK: Final synthesis reply
    SDK-->>WS: Turn completed (usage stats & cost metadata)
    WS-->>User: Append turn summary ($ spend, tokens, cache hit %)
```

---

## 📊 3. Data Science & Google Colab Workflow

The agent dynamically chooses between local sandboxed execution in `ds-env` and remote cloud acceleration in Google Colab:

```mermaid
flowchart TD
    Start(["User uploads dataset or assigns ML task"]) --> Ingest["Dataset placed in <workspace>/data/"]
    Ingest --> AutoEDA["Agent calls ds_preview(file_path)"]
    AutoEDA --> Decision{"Computational Intensity / Hardware Requirement?"}

    Decision -->|Lightweight / EDA / Econometrics| LocalExec["Local Execution in ds-env\n(ds_run / $DS_PYTHON)"]
    Decision -->|Heavy Deep Learning / Large Model / GPU Needed| ColabExec["Google Colab MCP Execution\n(colab_new -> colab_execute)"]

    subgraph LocalSandbox["Dedicated Data Science Sandbox (~/.coding-agent/ds-env)"]
        LocalExec --> Tools1["Pandas / Polars / Scipy / Scikit-Learn\nStatsmodels / Plotly / Matplotlib"]
        Tools1 --> PlotLocal["Save figure to /workspace/plot.png\nPrint '__ARTIFACT__:plot:/workspace/plot.png'"]
    end

    subgraph ColabSandbox["Google Colab Cloud Hardware"]
        ColabExec --> Runtimes["Provision T4 / L4 / A100 / TPU"]
        Runtimes --> RemoteCode["Execute training / inference remotely\nInstall packages via colab_install"]
        RemoteCode --> PlotRemote["Capture remote stdout & image base64 bytes"]
    end

    PlotLocal --> Stream["Stream back to browser via WebSocket"]
    PlotRemote --> Stream
    Stream --> Render["Browser automatically renders inline <img> & download cards"]
```

---

## 🔐 4. BYOK Security & Key Vault Architecture

ds-agent guarantees zero credential leakage to browser storage or plaintext logs through cryptographic key derivation:

```mermaid
flowchart LR
    subgraph Auth["Authentication & Key Derivation"]
        Pass["User APP_PASSWORD"] --> KDF["SHA-256 Hash\nurlsafe-b64 derivation"]
        KDF --> Key["32-byte Fernet Master Key"]
    end

    subgraph VaultStorage["On-Disk Encrypted Vault (~/.coding-agent/keys.enc)"]
        RawKeys["Provider Keys\n(OpenRouter, Anthropic, Kaggle, MiniMax)"]
        Key --> Encrypt["Fernet Encryption"]
        RawKeys --> Encrypt
        Encrypt --> EncFile[("keys.enc (AES-128-CBC + HMAC)")]
    end

    subgraph Runtime["Session Subprocess Initialization"]
        EncFile --> Decrypt["Fernet Decryption (In-Memory Only)"]
        Key --> Decrypt
        Decrypt --> Placeholders["Resolve ${VAULT:provider_key} in mcp.json\nand inject ANTHROPIC_AUTH_TOKEN into process env"]
        Placeholders --> Subprocess["Isolated Child Subprocess\n(No disk plaintext / no browser leakage)"]
    end
```

---

## 📈 5. Quantitative Research Architecture

ds-agent provides an integrated stack for econometric analysis, macroeconomic research, and financial modeling:

```mermaid
flowchart TD
    subgraph MacroData["Macroeconomic & Literature Ingestion"]
        FRED["FRED API\n(Interest Rates, CPI, Yields)"] --> RMCP["research_mcp (server.py)"]
        arXiv["arXiv (q-fin, stat, cs)"] --> RMCP
        Scholar["Semantic Scholar / CrossRef"] --> RMCP
    end

    subgraph QuantitativeStack["Quantitative Modeling & Econometrics Engine"]
        RMCP --> AgentCore["Agent Reasoning Loop"]
        AgentCore --> TS["Time-Series Analysis (statsmodels, scipy)\nARIMA, VAR, GARCH, Cointegration"]
        AgentCore --> Backtest["Strategy Backtesting (vectorbt, backtesting.py)\nSignal Generation & Portfolio Metrics"]
        AgentCore --> DataProc["Fast Vectorized Crunching (polars, pyarrow)"]
    end

    subgraph VisualOutput["Financial Artifact Delivery"]
        Backtest --> Charts["Plotly / Matplotlib Visualizations\n• Equity Curves\n• Underwater Drawdown Charts\n• Candlestick OHLCV\n• Monthly Return Heatmaps"]
        Charts --> Marker["__ARTIFACT__:plot:/path/to/chart.png"]
        Marker --> WebUI["Rendered Inline in Web Chat"]
    end
```

---

## 📁 6. Detailed File Layout

```
ds-agent/
├── src/
│   ├── ds_agent/              # The web server (FastAPI app package)
│   │   ├── app.py             # FastAPI entrypoint: UI pages, REST, WebSocket bridge
│   │   ├── core.py            # Config, paths, env loading (DATA_DIR, APP_PASSWORD, ...)
│   │   ├── db.py              # SQLite store (sessions, auth cookies, usage metrics)
│   │   ├── crypto.py          # BYOK key storage (Fernet-encrypted, password-derived)
│   │   ├── sessions.py        # Session lifecycle + ClaudeSDKClient subprocess management
│   │   ├── providers.py       # BYOK key → environment variables for subprocess
│   │   ├── model_catalog.py   # Curated model picker + live OpenRouter catalog sync
│   │   ├── agent_prompt.py    # Appended system prompt (data science & quant guidance)
│   │   ├── artifact_parser.py # __ARTIFACT__ marker parser → inline HTML/images/files
│   │   ├── trim.py            # Tool-result output trimmer (head/tail + pointer file)
│   │   ├── static/            # app.js, style.css (no build step, CDN libraries)
│   │   └── templates/         # Jinja2 HTML templates: base, index, login, settings
│   ├── colab_mcp/             # Stdio MCP server wrapping google-colab-cli
│   │   ├── colab_server.py    # 7 tools: auth, new, status, execute, install, sessions, stop
│   │   └── setup.sh           # Creates colab_mcp/.venv with google-colab-cli dependencies
│   ├── ds_mcp/                # Dedicated Data Science Environment MCP
│   │   └── server.py          # 4 tools: ds_preview, ds_run, ds_env, ds_install
│   └── research_mcp/          # Stdio MCP server, 12 academic/bio/quant search tools
│       └── server.py          # Tools for arXiv, FRED, PubMed, OpenAlex, HuggingFace, PDB
├── deploy/
│   └── Caddyfile              # 5-line Caddy reverse proxy with automated TLS
├── docs/                      # arch.md, todo.md, debug_notes.md
├── scripts/
│   ├── run_server.sh          # Start server on port 8765 with all environments
│   └── install_ds_env.sh      # Installer for dedicated ~/.coding-agent/ds-env
├── tests/                     # End-to-end WebSocket/HTTP test scripts + transcripts
├── mcp.json                   # Global MCP registry (copied to ~/.coding-agent/)
└── pyproject.toml             # uv-managed project dependencies
```

---

## 💾 7. Data Layout (`~/.coding-agent/`)

```
~/.coding-agent/
├── state.db                 # SQLite: sessions, auth_cookies, session_usage
├── keys.enc                 # Fernet-encrypted BYOK key vault (or keys.json fallback)
├── mcp.json                 # Global MCP registry
├── ds-env/                  # Dedicated Python sandbox for data science execution
├── sessions/<sid>/          # Transcripts (transcript.jsonl)
└── workspaces/<sid>/        # Per-session workspace (Claude CLI cwd, .mcp.json, data/)
    ├── data/                # Uploaded datasets (CSV, Parquet, JSON, etc.)
    └── .truncated/          # Large tool output overflow files
```

---

## ⚙️ 8. Key Components & Subsystem Details

### 1. Claude SDK Subprocess Management (`sessions.py`)
- Each session maintains an active `ClaudeSDKClient` instance kept warm across turns.
- Sessions preserve conversational context without re-transmitting previous history from scratch.
- An interrupt button sends an async interrupt event over WebSocket to abort runaway model generation.

### 2. Output Trimming & Memory Protection (`trim.py`)
- Tool outputs exceeding `TOOL_RESULT_MAX_BYTES` (default 30 KB) are automatically intercepted.
- The full raw output is preserved in `<workspace>/.truncated/<tool>-<hash>.txt`.
- The agent receives a truncated payload with the head (8 KB) and tail (4 KB), preventing context window blowouts.

### 3. History Reconstruction on Refresh
- When a user refreshes the browser, `GET /v1/sessions/{sid}/history` parses the on-disk SDK JSONL transcript.
- The parser reconstructs full message history and re-evaluates all `__ARTIFACT__` markers so embedded charts persist seamlessly across browser sessions.
