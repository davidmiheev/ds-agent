"""Core config, paths, env loading."""
from __future__ import annotations
import os
from pathlib import Path

# Project root = the repo checkout (this file lives in <root>/src/ds_agent/).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Data dir lives at ~/.coding-agent/ — override with CODING_AGENT_HOME for tests.
DATA_DIR = Path(os.environ.get("CODING_AGENT_HOME", str(Path.home() / ".coding-agent")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "sessions").mkdir(exist_ok=True)
(DATA_DIR / "workspaces").mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "state.db"
KEYS_PATH = DATA_DIR / "keys.enc"
VAULT_KEY_PATH = DATA_DIR / ".vault_key"
MCP_CONFIG_PATH = DATA_DIR / "mcp.json"

# Dedicated data-science Python env (created by scripts/install_ds_env.sh).
# The agent runs pandas/numpy/etc. through this interpreter, isolated from
# the server's own venv. DS_PYTHON is injected into every session's env.
DS_ENV_DIR = DATA_DIR / "ds-env"
DS_PYTHON = DS_ENV_DIR / "bin" / "python"


def ds_env_ready() -> bool:
    return DS_PYTHON.exists() and DS_PYTHON.is_file()

# Single-user password. Empty string → /login bypasses auth entirely.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

# Public-host hardening flag. When "1" the cookie is Secure and a real password
# is required; the can_use_tool callback also tightens tool approvals.
APP_PUBLIC = os.environ.get("APP_PUBLIC", "0") == "1"

# Session backend. local (default) = bare subprocess. docker = per-session container.
SESSION_BACKEND = os.environ.get("SESSION_BACKEND", "local")

# Hard cap per session so a runaway agent can't drain your OpenRouter wallet in one shot.
MAX_BUDGET_USD = float(os.environ.get("MAX_BUDGET_USD", "5.0"))

# Turn watchdog: if the SDK emits no message at all for this long (model call
# or MCP tool call hung with no response), interrupt the turn. Some providers
# (e.g. OpenRouter) can hold a request open indefinitely on a dead upstream.
TURN_INACTIVITY_TIMEOUT = float(os.environ.get("TURN_INACTIVITY_TIMEOUT", "300"))

# If an interrupt doesn't produce a result frame within this long, the client
# is force-disconnected and respawned so the session is usable again.
TURN_RECOVERY_TIMEOUT = float(os.environ.get("TURN_RECOVERY_TIMEOUT", "30"))

# Workspace entries never walked for search/export: .mcp.json and
# .claude/settings.local.json hold *resolved* provider API keys and BYOK
# secrets (see sessions._render_session_dir), not just config.
WORKSPACE_SECRET_DIRS = {".claude", ".git", "__pycache__", "node_modules", ".truncated"}
WORKSPACE_SECRET_FILES = {".mcp.json"}
