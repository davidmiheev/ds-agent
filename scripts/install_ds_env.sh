#!/usr/bin/env bash
# Create the dedicated data-science Python environment that the agent uses
# to run pandas / numpy / sklearn / plotting code.
#
# Works on any Linux box. Uses `uv` if available (fast), otherwise falls back
# to `python3 -m venv` + pip.
#
# Usage:
#   bash scripts/install_ds_env.sh            # env at ~/.coding-agent/ds-env
#   CODING_AGENT_HOME=/data bash scripts/install_ds_env.sh
#   DS_PY_VERSION=3.11 bash scripts/install_ds_env.sh   # pin interpreter
#
# Idempotent: re-running upgrades packages in place.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${CODING_AGENT_HOME:-$HOME/.coding-agent}"
ENV_DIR="$DATA_DIR/ds-env"
PY_VERSION="${DS_PY_VERSION:-3.12}"

# SOCKS proxy env vars break pip ("Missing dependencies for SOCKS support")
# when PySocks isn't installed. Unset them unless the user explicitly wants
# a proxy (KEEP_PROXY=1).
if [ "${KEEP_PROXY:-0}" != "1" ]; then
    unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY no_proxy NO_PROXY
fi

echo "==> ds-env"
echo "    location: $ENV_DIR"
echo "    python:   $PY_VERSION (override with DS_PY_VERSION=3.11 etc.)"

# ---------------------------------------------------------------- venv ------
if [ ! -x "$ENV_DIR/bin/python" ]; then
    if command -v uv >/dev/null 2>&1; then
        echo "==> creating venv with uv (python $PY_VERSION)"
        uv venv --python "$PY_VERSION" "$ENV_DIR"
    else
        echo "==> creating venv with python3 -m venv"
        python3 -m venv "$ENV_DIR"
    fi
else
    echo "==> venv already exists — upgrading packages"
fi

PY="$ENV_DIR/bin/python"
"$PY" -m ensurepip >/dev/null 2>&1 || true
"$PY" -m pip install --quiet --upgrade pip

# ------------------------------------------------------------- packages -----
# Core data stack. Kept deliberately lean; add more to DS_EXTRA_PACKAGES
# (space-separated) if you need e.g. polars, xgboost, torch.
PACKAGES=(
    "pandas"
    "numpy"
    "scipy"
    "scikit-learn"
    "statsmodels"
    "matplotlib"
    "seaborn"
    "plotly"
    "openpyxl"          # xlsx read/write for pandas
    "pyarrow"           # parquet
    "polars"
    "kaggle"            # kaggle CLI (datasets / competitions)
    "requests"
    "tqdm"
)
EXTRA="${DS_EXTRA_PACKAGES:-}"

echo "==> installing: ${PACKAGES[*]}${EXTRA:+ $EXTRA}"
# shellcheck disable=SC2086
"$PY" -m pip install --quiet --upgrade "${PACKAGES[@]}" $EXTRA

# ---------------------------------------------------------------- verify ----
echo "==> verifying"
"$PY" - <<'EOF'
import pandas, numpy, scipy, sklearn, statsmodels, matplotlib, seaborn, plotly, polars
print(f"    pandas      {pandas.__version__}")
print(f"    numpy       {numpy.__version__}")
print(f"    scipy       {scipy.__version__}")
print(f"    sklearn     {sklearn.__version__}")
print(f"    statsmodels {statsmodels.__version__}")
print(f"    matplotlib  {matplotlib.__version__}")
print(f"    seaborn     {seaborn.__version__}")
print(f"    plotly      {plotly.__version__}")
print(f"    polars      {polars.__version__}")
EOF

echo
echo "OK. The ds-agent server picks this up automatically (DS_PYTHON env var)."
echo "    Manual use: $PY -c \"import pandas; print(pandas.__version__)\""
