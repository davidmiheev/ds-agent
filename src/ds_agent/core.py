"""Core config, paths, env loading."""
from __future__ import annotations
import os
from pathlib import Path

# Data dir lives at ~/.coding-agent/ — override with CODING_AGENT_HOME for tests.
DATA_DIR = Path(os.environ.get("CODING_AGENT_HOME", str(Path.home() / ".coding-agent")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "sessions").mkdir(exist_ok=True)
(DATA_DIR / "workspaces").mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "state.db"
KEYS_PATH = DATA_DIR / "keys.enc"
MCP_CONFIG_PATH = DATA_DIR / "mcp.json"

# Single-user password. Empty string → /login bypasses auth entirely.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

# Public-host hardening flag. When "1" the cookie is Secure and a real password
# is required; the can_use_tool callback also tightens tool approvals.
APP_PUBLIC = os.environ.get("APP_PUBLIC", "0") == "1"

# Session backend. local (default) = bare subprocess. docker = per-session container.
SESSION_BACKEND = os.environ.get("SESSION_BACKEND", "local")

# Hard cap per session so a runaway agent can't drain your OpenRouter wallet in one shot.
MAX_BUDGET_USD = float(os.environ.get("MAX_BUDGET_USD", "5.0"))
