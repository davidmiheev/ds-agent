#!/usr/bin/env bash
# Set up the colab-mcp Python venv and install dependencies.
#
# Run once after cloning the project:
#   bash src/colab_mcp/setup.sh
#
# Then point mcp.json at the absolute path of venv/bin/python + colab_server.py.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VENV="$HERE/.venv"

if [ "${KEEP_PROXY:-0}" != "1" ]; then
    unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY no_proxy NO_PROXY
fi

if [ ! -d "$VENV" ]; then
    echo "==> creating venv at $VENV (Python 3.13)"
    uv venv --python 3.13 "$VENV"
fi

PY="$VENV/bin/python"

echo "==> cloning google-colab-cli if needed"
CLONE_DIR="${COLAB_CLI_SRC:-/tmp/google-colab-cli}"
if [ ! -d "$CLONE_DIR" ]; then
    echo "    cloning to $CLONE_DIR"
    git clone --depth 1 https://github.com/googlecolab/google-colab-cli "$CLONE_DIR"
fi

echo "==> installing google-colab-cli and runtime dependencies via uv"
# NOTE: colab_server.py uses the MCP 1.x API (Server + @server.list_tools()).
# Pin mcp<2 — the 2.x API removed those and would break the server at import.
if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PY" -e "$CLONE_DIR"
    uv pip install --python "$PY" \
        "jupyter-kernel-client" "mcp[cli]<2" "typer" "rich" \
        "pydantic>=2.0" "requests" "google-auth" "google-auth-oauthlib" \
        "nbformat" "filelock"
else
    "$PY" -m ensurepip >/dev/null 2>&1 || true
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -e "$CLONE_DIR"
    "$PY" -m pip install --quiet \
        "jupyter-kernel-client" "mcp[cli]<2" "typer" "rich" \
        "pydantic>=2.0" "requests" "google-auth" "google-auth-oauthlib" \
        "nbformat" "filelock"
fi

echo
echo "OK. Colab MCP venv ready at:"
echo "  $PY $HERE/colab_server.py"
