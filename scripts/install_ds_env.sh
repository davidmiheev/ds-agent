#!/usr/bin/env bash
# Create the dedicated data-science Python environment that the agent uses
# to run pandas / numpy / sklearn / plotting / quant research code.
#
# Works on any Linux box. Uses `uv` if available (fast), otherwise falls back
# to `python3 -m venv` + pip.
#
# Usage:
#   bash scripts/install_ds_env.sh                      # default core data science env
#   INSTALL_QUANT=1 bash scripts/install_ds_env.sh      # core + quant research packages
#   DS_PRESET=quant bash scripts/install_ds_env.sh      # alias for quant preset
#   CODING_AGENT_HOME=/data bash scripts/install_ds_env.sh
#   DS_PY_VERSION=3.11 bash scripts/install_ds_env.sh   # pin interpreter
#
# Idempotent: re-running upgrades packages in place.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${CODING_AGENT_HOME:-$HOME/.coding-agent}"
ENV_DIR="$DATA_DIR/ds-env"
PY_VERSION="${DS_PY_VERSION:-3.12}"

# SOCKS proxy env vars break pip/uv when PySocks isn't installed.
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

# ------------------------------------------------------------- packages -----
# Core data stack.
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

# Quantitative research packages preset
QUANT_PACKAGES=(
    "yfinance"          # market data & OHLCV prices
    "pandas-ta"         # technical analysis indicators
    "vectorbt"          # high-performance vectorized backtesting
    "backtesting"       # event-driven strategy backtesting
    "arch"              # autoregressive conditional heteroskedasticity & volatility
    "quantstats"        # portfolio performance & risk analytics tearsheets
)

PRESET="${DS_PRESET:-}"
if [ "${INSTALL_QUANT:-0}" = "1" ] || [ "$PRESET" = "quant" ] || [ "$PRESET" = "all" ]; then
    echo "==> including quantitative research preset: ${QUANT_PACKAGES[*]}"
    PACKAGES+=("${QUANT_PACKAGES[@]}")
fi

EXTRA="${DS_EXTRA_PACKAGES:-}"

echo "==> installing: ${PACKAGES[*]}${EXTRA:+ $EXTRA}"
if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PY" "${PACKAGES[@]}" $EXTRA
else
    "$PY" -m ensurepip >/dev/null 2>&1 || true
    "$PY" -m pip install --quiet --upgrade pip
    # shellcheck disable=SC2086
    "$PY" -m pip install --quiet --upgrade "${PACKAGES[@]}" $EXTRA
fi

# ---------------------------------------------------------------- verify ----
echo "==> verifying"
"$PY" - <<'EOFVERIFY'
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

# Check optional quant packages if installed
quant_mods = ["yfinance", "pandas_ta", "vectorbt", "backtesting", "arch", "quantstats"]
for mod_name in quant_mods:
    try:
        mod = __import__(mod_name)
        ver = getattr(mod, "__version__", "installed")
        print(f"    {mod_name:<11} {ver}")
    except ImportError:
        pass
EOFVERIFY

echo
echo "OK. The ds-agent server picks this up automatically (DS_PYTHON env var)."
echo "    Manual use: $PY -c \"import pandas; print(pandas.__version__)\""
