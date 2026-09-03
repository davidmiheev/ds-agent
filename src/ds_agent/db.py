"""SQLite store. One connection per request, WAL mode for safe concurrency."""
from __future__ import annotations
import sqlite3
import time
from contextlib import contextmanager
from . import core

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    base_url        TEXT,
    mcp_overrides   TEXT NOT NULL DEFAULT '{}',
    workspace       TEXT NOT NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_cookies (
    token       TEXT PRIMARY KEY,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_usage (
    session_id      TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    last_usage      TEXT,        -- JSON: {input_tokens, output_tokens, cache_read, cache_creation, cost_usd, turn_count}
    last_at         REAL
);
CREATE TABLE IF NOT EXISTS login_otp (
    id          INTEGER PRIMARY KEY CHECK (id = 1),  -- single-user app: at most one live code
    code_hash   TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    tags        TEXT,
    session_id  TEXT,
    created_at  REAL NOT NULL
);
"""

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(core.DB_PATH, isolation_level=None, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn

@contextmanager
def conn():
    c = _connect()
    try:
        yield c
    finally:
        c.close()

def init() -> None:
    with conn() as c:
        c.executescript(_SCHEMA)

def create_session(s: dict) -> None:
    s.setdefault("created_at", time.time())
    s["updated_at"] = time.time()
    with conn() as c:
        c.execute(
            """INSERT INTO sessions (id, title, provider, model, base_url, mcp_overrides, workspace, created_at, updated_at)
               VALUES (:id, :title, :provider, :model, :base_url, :mcp_overrides, :workspace, :created_at, :updated_at)""",
            s,
        )

def get_session(sid: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        return dict(row) if row else None

def list_sessions() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()]

def touch_session(sid: str) -> None:
    with conn() as c:
        c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), sid))

def update_title(sid: str, title: str) -> None:
    with conn() as c:
        c.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?", (title, time.time(), sid))

def delete_session(sid: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM sessions WHERE id=?", (sid,))

def make_cookie_token() -> str:
    import secrets
    tok = secrets.token_urlsafe(32)
    with conn() as c:
        c.execute("INSERT INTO auth_cookies (token, created_at) VALUES (?, ?)", (tok, time.time()))
    return tok

def check_cookie(tok: str) -> bool:
    """Whether `tok` is a valid, issued session cookie.

    Whether a cookie is required at all (no auth configured vs. password vs.
    Telegram OTP) is an app.py concern — see app._auth_required().
    """
    if not tok:
        return False
    with conn() as c:
        return c.execute("SELECT 1 FROM auth_cookies WHERE token=?", (tok,)).fetchone() is not None


# ---- Telegram OTP web login ----

def set_login_otp(code_hash: str, ttl_seconds: float) -> None:
    now = time.time()
    with conn() as c:
        c.execute(
            """INSERT INTO login_otp (id, code_hash, created_at, expires_at, attempts)
               VALUES (1, ?, ?, ?, 0)
               ON CONFLICT(id) DO UPDATE SET
                   code_hash=excluded.code_hash, created_at=excluded.created_at,
                   expires_at=excluded.expires_at, attempts=0""",
            (code_hash, now, now + ttl_seconds),
        )

def get_login_otp() -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM login_otp WHERE id=1").fetchone()
        return dict(row) if row else None

def bump_otp_attempts() -> int:
    with conn() as c:
        c.execute("UPDATE login_otp SET attempts = attempts + 1 WHERE id=1")
        row = c.execute("SELECT attempts FROM login_otp WHERE id=1").fetchone()
        return row["attempts"] if row else 0

def clear_login_otp() -> None:
    with conn() as c:
        c.execute("DELETE FROM login_otp WHERE id=1")


# ---- cross-session agent memory ----
# A small persistent notebook the agent itself reads/writes via the memory
# MCP tools (remember/recall/forget) — shared across every session, not
# scoped to one workspace. See agent_mcp.py.

def add_memory(text: str, tags: str = "", session_id: str | None = None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO memories (text, tags, session_id, created_at) VALUES (?, ?, ?, ?)",
            (text, tags, session_id, time.time()),
        )
        return cur.lastrowid

def list_memories(query: str = "", limit: int = 50) -> list[dict]:
    with conn() as c:
        if query:
            like = f"%{query}%"
            rows = c.execute(
                "SELECT * FROM memories WHERE text LIKE ? OR tags LIKE ? ORDER BY created_at DESC LIMIT ?",
                (like, like, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

def delete_memory(memory_id: int) -> bool:
    with conn() as c:
        cur = c.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        return cur.rowcount > 0


# ---- session usage tracking (last turn) ----

def record_usage(sid: str, usage: dict) -> None:
    import json as _json
    with conn() as c:
        c.execute(
            """INSERT INTO session_usage (session_id, last_usage, last_at)
               VALUES (?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET last_usage=excluded.last_usage, last_at=excluded.last_at""",
            (sid, _json.dumps(usage), time.time()),
        )

def get_usage(sid: str) -> dict | None:
    import json as _json
    with conn() as c:
        row = c.execute("SELECT last_usage, last_at FROM session_usage WHERE session_id=?", (sid,)).fetchone()
        if not row:
            return None
        return {"usage": _json.loads(row["last_usage"]) if row["last_usage"] else None, "last_at": row["last_at"]}
