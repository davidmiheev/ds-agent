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

if [ ! -d "$VENV" ]; then
    echo "==> creating venv at $VENV (Python 3.13)"
    uv venv --python 3.13 "$VENV"
fi

# ensure pip
"$VENV/bin/python" -m ensurepip >/dev/null 2>&1 || true

echo "==> installing google-colab-cli (editable from local clone)"
CLONE_DIR="${COLAB_CLI_SRC:-/tmp/google-colab-cli}"
if [ ! -d "$CLONE_DIR" ]; then
    echo "    cloning $CLONE_DIR"
    git config --global http.sslVerify false || true
    git clone --depth 1 https://github.com/googlecolab/google-colab-cli "$CLONE_DIR"
fi
"$VENV/bin/python" -m pip install --quiet --index-url https://pypi.org/simple -e "$CLONE_DIR"

echo "==> installing runtime dependencies"
"$VENV/bin/python" -m pip install --quiet --index-url https://pypi.org/simple \
    "jupyter-kernel-client" "mcp[cli]" "typer" "rich" \
    "pydantic>=2.0" "requests" "google-auth" "google-auth-oauthlib" \
    "nbformat" "filelock"

echo
echo "OK. Run the MCP server with:"
echo "  $VENV/bin/python $HERE/colab_server.py"
