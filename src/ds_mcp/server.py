"""MCP server for the dedicated data-science Python environment.

The agent (claude) runs data work through this server instead of the bare
system python, so pandas / numpy / sklearn / plotting are always available
in a known, reproducible interpreter.

Tools:
  ds_env      — show the DS env status (python path, key package versions)
  ds_run      — run a python script file or inline code in the DS env
  ds_preview  — load a dataset (csv/parquet/xlsx/json) and return shape,
                dtypes, head rows, and basic describe stats
  ds_install  — pip-install extra packages into the DS env

Env vars (set in mcp.json):
  DS_PYTHON      — path to the DS env interpreter
                   (default ~/.coding-agent/ds-env/bin/python)
  DS_RUN_TIMEOUT — seconds before ds_run is killed (default 600)

NOTE: written against the mcp 2.x API (MCPServer + @server.tool). The
research_mcp / colab_mcp servers use the older 1.x decorator API and run on
a separate venv — do not mix the two styles.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path

from mcp.server import MCPServer

import logging

LOG = logging.getLogger("ds-mcp")
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

DS_PYTHON = os.environ.get(
    "DS_PYTHON", str(Path.home() / ".coding-agent" / "ds-env" / "bin" / "python")
)
DS_RUN_TIMEOUT = int(os.environ.get("DS_RUN_TIMEOUT", "600"))

server = MCPServer("ds-mcp")


def _env_missing() -> str:
    return json.dumps({
        "error": f"DS env not found at {DS_PYTHON}. "
                 "Run `bash scripts/install_ds_env.sh` on the host, then retry."
    })


def _run_py(code: str, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run([DS_PYTHON, "-c", code], capture_output=True,
                          text=True, timeout=timeout)


# ---------------- tools ----------------

@server.tool()
def ds_env() -> str:
    """Show the dedicated data-science Python environment: interpreter path
    and versions of pandas/numpy/scipy/sklearn/statsmodels/matplotlib/
    seaborn/plotly/polars. Call this first to confirm the env is ready."""
    if not Path(DS_PYTHON).exists():
        return _env_missing()
    code = (
        "import json,sys\n"
        "mods=['pandas','numpy','scipy','sklearn','statsmodels','matplotlib',"
        "'seaborn','plotly','polars','pyarrow']\n"
        "out={'python':sys.executable,'version':sys.version.split()[0],'packages':{}}\n"
        "for m in mods:\n"
        "    try:\n"
        "        mod=__import__(m); out['packages'][m]=getattr(mod,'__version__','?')\n"
        "    except Exception as e: out['packages'][m]=f'MISSING: {e}'\n"
        "print(json.dumps(out))\n"
    )
    try:
        r = _run_py(code, timeout=60)
    except Exception as e:
        return json.dumps({"error": str(e)})
    if r.returncode != 0:
        return json.dumps({"error": r.stderr.strip()[:2000]})
    return r.stdout.strip().splitlines()[-1]


@server.tool()
def ds_run(code: str = "", script: str = "", timeout: int = 0) -> str:
    """Run Python code in the dedicated data-science environment (pandas,
    numpy, scipy, scikit-learn, statsmodels, matplotlib, seaborn, plotly,
    polars, pyarrow preinstalled). Provide either `code` (inline python) or
    `script` (path to a .py file, relative to the workspace). Returns
    stdout/stderr/returncode. Default timeout 600s."""
    if not Path(DS_PYTHON).exists():
        return _env_missing()
    if bool(code) == bool(script):
        return json.dumps({"error": "provide exactly one of `code` or `script`"})
    timeout = int(timeout or DS_RUN_TIMEOUT)

    if script:
        sp = Path(script)
        if not sp.is_absolute():
            sp = Path.cwd() / sp
        if not sp.exists():
            return json.dumps({"error": f"script not found: {sp}"})
        cmd = [DS_PYTHON, str(sp)]
    else:
        cmd = [DS_PYTHON, "-c", code]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=str(Path.cwd()))
    except subprocess.TimeoutExpired:
        return json.dumps({"error": f"timed out after {timeout}s", "timeout": timeout})
    except Exception as e:
        return json.dumps({"error": str(e)})

    out = {
        "returncode": r.returncode,
        "stdout": r.stdout[-20000:],   # cap so huge prints don't blow the context
        "stderr": r.stderr[-5000:],
    }
    if len(r.stdout) > 20000:
        out["stdout_truncated"] = True
    return json.dumps(out)


@server.tool()
def ds_preview(path: str, rows: int = 10) -> str:
    """Load a dataset (csv/parquet/xlsx/json) and return shape, per-column
    dtype/nulls/unique counts, head rows, and describe stats. Use this FIRST
    when given a new dataset. `path` may be relative to the workspace."""
    if not Path(DS_PYTHON).exists():
        return _env_missing()
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        return json.dumps({"error": f"file not found: {p}"})

    code = f"""
import json, sys
from pathlib import Path
import pandas as pd

p = Path({str(p)!r})
rows = int({int(rows)})
ext = p.suffix.lower()
try:
    if ext == '.parquet':
        df = pd.read_parquet(p)
    elif ext in ('.xlsx', '.xls'):
        df = pd.read_excel(p)
    elif ext == '.json':
        df = pd.read_json(p)
    else:
        df = pd.read_csv(p)
except Exception as e:
    print(json.dumps({{"error": f"failed to read: {{e}}"}}))
    sys.exit(0)

def _clean(v):
    try:
        json.dumps(v); return v
    except Exception:
        return str(v)

out = {{
    "file": str(p),
    "shape": list(df.shape),
    "columns": [{{"name": c, "dtype": str(df[c].dtype),
                  "nulls": int(df[c].isna().sum()),
                  "unique": int(df[c].nunique())}} for c in df.columns],
    "head": json.loads(df.head(rows).to_json(orient="records", date_format="iso")),
}}
try:
    out["describe"] = json.loads(df.describe(include="all").to_json(orient="index"))
except Exception:
    pass
print(json.dumps(out, default=_clean))
"""
    try:
        r = _run_py(code, timeout=120)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "timed out after 120s (file too large?)"})
    if r.returncode != 0:
        return json.dumps({"error": r.stderr.strip()[:2000]})
    return r.stdout.strip().splitlines()[-1]


@server.tool()
def ds_install(packages: list[str]) -> str:
    """pip-install extra packages into the data-science environment (e.g.
    xgboost, lightgbm, torch). Use only when a needed package is missing."""
    if not Path(DS_PYTHON).exists():
        return _env_missing()
    if not packages:
        return json.dumps({"error": "no packages given"})
    try:
        r = subprocess.run(
            [DS_PYTHON, "-m", "pip", "install", "--quiet", "--upgrade", *packages],
            capture_output=True, text=True, timeout=900,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "timed out after 900s"})
    return json.dumps({
        "returncode": r.returncode,
        "installed": packages,
        "output": (r.stdout + r.stderr)[-3000:],
    })


def main() -> None:
    import asyncio
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
