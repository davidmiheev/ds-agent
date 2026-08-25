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

# ---------------------------------------------------------------------------
# Auto-resolve a busy port: if something is already bound to $PORT, find the
# owning PID(s) and kill them so uvicorn can bind cleanly.
#
#   KILL_PORT=0  disable this behaviour (fail fast on a busy port instead)
#
# Uses `ss` first (present on most modern distros), then falls back to `lsof`,
# then to a /proc scan (no external deps). Only kills listeners on $HOST:$PORT.
# ---------------------------------------------------------------------------
_free_port() {
    local port="$1"
    if [ "${KILL_PORT:-1}" = "0" ]; then
        return 0
    fi

    # Collect PIDs listening on the port.
    local pids=""
    if command -v ss >/dev/null 2>&1; then
        # ss -ltnp: listening, numeric, show process. Match ":<port>" in the
        # local-address column. -H hides the header line.
        # `|| true`: a free port means grep finds no match (exit 1); without
        # this, `set -euo pipefail` would abort the whole script on the
        # common case where the port is already free.
        pids="$(ss -ltnpH 2>/dev/null \
            | awk -v p=":$port" '$4 ~ p"$" {print $0}' \
            | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u || true)"
    fi
    if [ -z "$pids" ] && command -v lsof >/dev/null 2>&1; then
        pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u || true)"
    fi
    if [ -z "$pids" ] && [ -d /proc ]; then
        # /proc fallback: parse the inode of the listening socket, then find
        # which process holds it. Inode for port P is the hex of P in the
        # "local_address" field of /proc/net/tcp{,6}.
        local hex_port inodes ino fd p
        hex_port="$(printf '%04X' "$port")"
        inodes="$(awk -v hp=":$hex_port" '
                    NR>1 && $2 ~ hp"$" && $4 == "0A" {print $10}' \
                    /proc/net/tcp /proc/net/tcp6 2>/dev/null | sort -u)"
        for ino in $inodes; do
            for fd in /proc/[0-9]*/fd/*; do
                if [ "$(readlink "$fd" 2>/dev/null)" = "socket:[$ino]" ]; then
                    p="$(echo "$fd" | cut -d/ -f3)"
                    pids="$pids $p"
                fi
            done
        done
        pids="$(echo "$pids" | tr ' ' '\n' | sort -u | grep -v '^$' | tr '\n' ' ' || true)"
    fi

    # Nothing found → port is free (or we can't see it); nothing to do.
    [ -z "$pids" ] && return 0

    for p in $pids; do
        # Never kill ourselves or our parent.
        [ "$p" = "$$" ] && continue
        [ "$p" = "$PPID" ] && continue
        local cmd
        cmd="$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null || echo "?")"
        echo "==> port $port in use by PID $p: ${cmd:0:80}"
        kill "$p" 2>/dev/null || true
    done

    # Wait up to ~5s for the socket to actually release.
    local i
    for i in 1 2 3 4 5; do
        if ! (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
            echo "==> port $port freed"
            return 0
        fi
        exec 3>&- 2>/dev/null || true
        sleep 1
    done

    echo "==> port $port still busy after SIGTERM; sending SIGKILL" >&2
    for p in $pids; do
        [ "$p" = "$$" ] && continue
        kill -9 "$p" 2>/dev/null || true
    done
    sleep 1
}

_free_port "$PORT"

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
