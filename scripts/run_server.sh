#!/usr/bin/env bash
# Start the ds-agent web server on port 8765 with all necessary env vars.
#
# Usage:
#   bash scripts/run_server.sh            # foreground, port 8765
#   PORT=9000 bash scripts/run_server.sh  # override port
#
# Env vars (all optional, sensible defaults below):
#   OPENROUTER_API_KEY  auto-seeded as the 'openrouter' BYOK key at startup
#                       (only if no key is stored yet — Settings page wins).
#   APP_PASSWORD        single-user login password. Empty → no-password mode.
#                       If a .env file exists at the project root, it is sourced.
#   APP_PUBLIC          "1" → public-host hardening (Secure cookies, password required)
#   SESSION_BACKEND     "local" (default) | "docker"
#   MAX_BUDGET_USD      hard per-session spend cap (default 5.0)
#   CODING_AGENT_HOME   data dir (default ~/.coding-agent)
#   TOOL_RESULT_MAX_BYTES / TOOL_RESULT_KEEP_HEAD / TOOL_RESULT_KEEP_TAIL
#                       tool-output trimming knobs (see src/ds_agent/trim.py)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Load .env if present (KEY=VALUE lines; comments allowed)
if [ -f "$ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$ROOT/.env"
    set +a
fi

# Defaults — only applied when not already set (env / .env wins)
export APP_PASSWORD="${APP_PASSWORD:-}"
export APP_PUBLIC="${APP_PUBLIC:-0}"
export SESSION_BACKEND="${SESSION_BACKEND:-local}"
export MAX_BUDGET_USD="${MAX_BUDGET_USD:-5.0}"
export CODING_AGENT_HOME="${CODING_AGENT_HOME:-$HOME/.coding-agent}"

# Make src/ importable so `ds_agent`, `colab_mcp`, `research_mcp` resolve
# even if the package isn't pip-installed into the venv.
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

PORT="${PORT:-8765}"
HOST="${HOST:-127.0.0.1}"

echo "==> ds-agent server"
echo "    url:      http://$HOST:$PORT"
echo "    data dir: $CODING_AGENT_HOME"
echo "    password: ${APP_PASSWORD:+set}${APP_PASSWORD:-<none — no-password mode>}"
echo "    public:   $APP_PUBLIC   backend: $SESSION_BACKEND   budget: \$${MAX_BUDGET_USD}"

# Prefer the project venv directly (works offline); fall back to `uv run`,
# which re-syncs deps first (needs network on first run / after dep changes).
if [ -x "$ROOT/.venv/bin/uvicorn" ]; then
    exec "$ROOT/.venv/bin/uvicorn" ds_agent.app:app --host "$HOST" --port "$PORT"
elif command -v uv >/dev/null 2>&1; then
    exec uv run uvicorn ds_agent.app:app --host "$HOST" --port "$PORT"
else
    echo "error: no $ROOT/.venv/bin/uvicorn and no uv on PATH — run 'uv sync' first" >&2
    exit 1
fi
