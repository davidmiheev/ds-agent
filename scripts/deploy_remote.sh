#!/usr/bin/env bash
# Master deploy script: install ds-agent on a remote Ubuntu/Debian host so the
# agent is fully ready to run (app + all MCP servers + systemd service).
#
# Usage:
#   bash scripts/deploy_remote.sh [REMOTE_IP] [SSH_KEY_PATH]
#
# Both arguments are optional — missing values are asked for interactively.
#
# Env overrides (all optional):
#   REMOTE_USER   remote ssh user            (default: root)
#   REMOTE_PORT   remote ssh port            (default: 22)
#   REMOTE_DIR    install dir on remote      (default: /opt/coding-agent)
#   APP_PORT      web server port            (default: 8765)
#
# What it does:
#   1. rsyncs the project (excludes .git, venvs) + scp's .env (chmod 600)
#   2. installs Node.js (npx) if missing — needed by filesystem & kaggle MCPs
#   3. creates a non-root 'agent' user (the CLI refuses
#      --dangerously-skip-permissions under root)
#   4. installs uv for the agent user, builds .venv (uv sync)
#   5. builds the colab MCP venv (src/colab_mcp/setup.sh)
#   6. builds the data-science env (scripts/install_ds_env.sh)
#   7. installs the live MCP config to ~/.coding-agent/mcp.json
#   8. installs + starts the systemd service (coding-agent.service)
#   9. health-checks /healthz and prints the URL
#
# Idempotent: safe to re-run (re-syncs files, upgrades venvs in place).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ------------------------------------------------------------------ args ----
REMOTE_IP="${1:-}"
SSH_KEY="${2:-}"
REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_PORT="${REMOTE_PORT:-22}"
REMOTE_DIR="${REMOTE_DIR:-/opt/coding-agent}"
APP_PORT="${APP_PORT:-8765}"

if [ -z "$REMOTE_IP" ]; then
    read -rp "Remote host IP: " REMOTE_IP
fi
if [ -z "$SSH_KEY" ]; then
    read -rp "Path to SSH private key: " SSH_KEY
fi

[ -f "$SSH_KEY" ]  || { echo "ERROR: SSH key not found: $SSH_KEY" >&2; exit 1; }
[ -f "$ROOT/.env" ] || { echo "ERROR: .env not found at project root: $ROOT/.env" >&2; exit 1; }

SSH="ssh -i $SSH_KEY -p $REMOTE_PORT -o StrictHostKeyChecking=accept-new ${REMOTE_USER}@${REMOTE_IP}"
SCP="scp -i $SSH_KEY -P $REMOTE_PORT"

echo "==> Deploying ds-agent to ${REMOTE_USER}@${REMOTE_IP}:${REMOTE_DIR} (port $APP_PORT)"

# ------------------------------------------------------------- 1. files -----
echo "==> [1/6] Copying project files (rsync)"
$SSH "mkdir -p $REMOTE_DIR"
rsync -az --delete \
    -e "ssh -i $SSH_KEY -p $REMOTE_PORT -o StrictHostKeyChecking=accept-new" \
    --exclude '.git' --exclude '.venv' --exclude 'src/colab_mcp/.venv' \
    --exclude '__pycache__' --exclude '.coding-agent' \
    ./ "${REMOTE_USER}@${REMOTE_IP}:${REMOTE_DIR}/"

echo "==> [2/6] Copying .env (chmod 600)"
$SCP "$ROOT/.env" "${REMOTE_USER}@${REMOTE_IP}:${REMOTE_DIR}/.env"
$SSH "chmod 600 ${REMOTE_DIR}/.env"

# ------------------------------------------------------- 3-8. remote setup --
echo "==> [3/6] Remote setup: node, agent user, venvs, MCPs, systemd"

REMOTE_SCRIPT="$(mktemp /tmp/dsagent_remote_setup.XXXXXX.sh)"
trap 'rm -f "$REMOTE_SCRIPT"' EXIT
cat > "$REMOTE_SCRIPT" <<REMOTE_EOF
#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
REMOTE_DIR="$REMOTE_DIR"
APP_PORT="$APP_PORT"

echo "--- [a] Node.js (needed by filesystem + kaggle MCPs)"
if ! command -v node >/dev/null 2>&1; then
    apt-get update -y
    apt-get install -y nodejs npm
fi
node --version

echo "--- [b] non-root 'agent' user (CLI refuses --dangerously-skip-permissions as root)"
id agent >/dev/null 2>&1 || useradd -r -m -d /home/agent -s /bin/bash agent
chown -R agent:agent "\$REMOTE_DIR"
chown agent:agent "\$REMOTE_DIR/.env"
chmod 600 "\$REMOTE_DIR/.env"

echo "--- [c] uv for the agent user"
su - agent -c 'command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh'

echo "--- [d] app venv + dependencies (uv sync)"
su - agent -c "export PATH=\"/home/agent/.local/bin:\$PATH\"; cd \"\$REMOTE_DIR\" && uv venv .venv && uv sync"

echo "--- [e] colab MCP venv"
su - agent -c "export PATH=\"/home/agent/.local/bin:\$PATH\"; bash \"\$REMOTE_DIR/src/colab_mcp/setup.sh\""

echo "--- [f] data-science env (ds-env)"
su - agent -c "export PATH=\"/home/agent/.local/bin:\$PATH\"; bash \"\$REMOTE_DIR/scripts/install_ds_env.sh\""

echo "--- [g] live MCP config -> /home/agent/.coding-agent/mcp.json"
mkdir -p /home/agent/.coding-agent
install -o agent -g agent -m 644 "\$REMOTE_DIR/mcp.json" /home/agent/.coding-agent/mcp.json

echo "--- [h] systemd service"
cat > /etc/systemd/system/coding-agent.service <<'UNIT'
[Unit]
Description=ds-agent coding agent web server
After=network.target

[Service]
Type=simple
WorkingDirectory=__REMOTE_DIR__
User=agent
Group=agent
Environment=PATH=/home/agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=PYTHONPATH=__REMOTE_DIR__/src
EnvironmentFile=__REMOTE_DIR__/.env
ExecStart=__REMOTE_DIR__/.venv/bin/uvicorn ds_agent.app:app --host 0.0.0.0 --port __APP_PORT__
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
sed -i "s|__REMOTE_DIR__|$REMOTE_DIR|g; s|__APP_PORT__|$APP_PORT|g" /etc/systemd/system/coding-agent.service
systemctl daemon-reload
systemctl enable --now coding-agent

echo "--- [i] health check"
ok=""
for i in \$(seq 1 30); do
    if curl -sf "http://127.0.0.1:$APP_PORT/healthz" >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
done
[ -n "\$ok" ] || { echo "ERROR: server did not become healthy" >&2; journalctl -u coding-agent -n 30 --no-pager >&2; exit 1; }
echo "HEALTHY"
REMOTE_EOF

$SCP "$REMOTE_SCRIPT" "${REMOTE_USER}@${REMOTE_IP}:/tmp/dsagent_remote_setup.sh"
$SSH "bash /tmp/dsagent_remote_setup.sh && rm -f /tmp/dsagent_remote_setup.sh"

# ----------------------------------------------------------------- done -----
echo "==> [4/6] Verifying MCP servers (initialize handshake)"
$SSH "su - agent -c '
export PATH=\$HOME/.local/bin:\$PATH
export PYTHONPATH=$REMOTE_DIR/src
export DS_PYTHON=/home/agent/.coding-agent/ds-env/bin/python
cd $REMOTE_DIR
INIT=\"{\\\"jsonrpc\\\":\\\"2.0\\\",\\\"id\\\":1,\\\"method\\\":\\\"initialize\\\",\\\"params\\\":{\\\"protocolVersion\\\":\\\"2024-11-05\\\",\\\"capabilities\\\":{},\\\"clientInfo\\\":{\\\"name\\\":\\\"t\\\",\\\"version\\\":\\\"1\\\"}}}\"
NOTIF=\"{\\\"jsonrpc\\\":\\\"2.0\\\",\\\"method\\\":\\\"notifications/initialized\\\"}\"
check() {
    local name=\"\$1\"; shift
    if (printf \"%s\n%s\n\" \"\$INIT\" \"\$NOTIF\" | timeout 180 \$@ 2>/dev/null | head -1) | grep -q serverInfo; then
        echo \"  \$name: OK\"
    else
        echo \"  \$name: FAIL\"
    fi
}
check filesystem npx -y @modelcontextprotocol/server-filesystem /tmp
check colab      src/colab_mcp/.venv/bin/python -m colab_mcp.colab_server
check research   .venv/bin/python -m research_mcp.server
check ds         .venv/bin/python -m ds_mcp.server
'" || echo "  (MCP verification had failures — check output above)"

echo "==> [5/6] Verifying kaggle remote MCP (Bearer token from .env)"
$SSH "su - agent -c '
export PATH=\$HOME/.local/bin:\$PATH
TOK=\$(grep ^KAGGLE_API_TOKEN $REMOTE_DIR/.env | cut -d= -f2)
if [ -n \"\$TOK\" ]; then
    (printf \"%s\n\" \"{\\\"jsonrpc\\\":\\\"2.0\\\",\\\"id\\\":1,\\\"method\\\":\\\"initialize\\\",\\\"params\\\":{\\\"protocolVersion\\\":\\\"2024-11-05\\\",\\\"capabilities\\\":{},\\\"clientInfo\\\":{\\\"name\\\":\\\"t\\\",\\\"version\\\":\\\"1\\\"}}}\"; sleep 25) \
        | timeout 120 npx -y mcp-remote https://www.kaggle.com/mcp --header "Authorization: Bearer \$TOK" 2>/dev/null \
        | grep -m1 -o \"\\\"serverInfo\\\": *{[^}]*}\" && echo \"  kaggle: OK\" || echo \"  kaggle: FAIL\"
else
    echo \"  kaggle: SKIP (no KAGGLE_API_TOKEN in .env)\"
fi
'" || true

echo "==> [6/6] Done"
echo
echo "  ds-agent is live:  http://${REMOTE_IP}:${APP_PORT}"
echo "  service:           systemctl status coding-agent   (on the remote)"
echo "  logs:              journalctl -u coding-agent -f"
echo "  data dir:          /home/agent/.coding-agent/"
echo
echo "  Note: colab MCP needs a one-time Google OAuth (colab_auth tool) on first use."
