"""MCP server giving the agent itself two cross-session abilities:

1. Search other sessions' messages + workspace artifacts for context
   (search_other_sessions, get_session_summary) — useful when the user
   references prior work ("the model I trained yesterday", "that dataset
   from the other session") so the agent can find and reuse it instead of
   redoing it from scratch.
2. A small persistent memory notebook (remember/recall/forget) that
   survives across every session, not just the current workspace. The most
   recent memories are also injected into every new session's system prompt
   (see agent_prompt.py) so the agent doesn't have to call recall() just to
   know what it already knows.

Runs in the main .venv, same as ds_mcp/research_mcp (mcp 2.x API:
MCPServer + @server.tool). The claude CLI spawns MCP subprocesses with
cwd = the session's own workspace dir
(~/.coding-agent/workspaces/<sid> — see docs/debug_notes.md), so
Path.cwd().name recovers "our own" session id with no extra plumbing.
"""
from __future__ import annotations
import json
import logging
import sys
from pathlib import Path

from mcp.server import MCPServer

from . import db, search as search_mod, sessions

LOG = logging.getLogger("agent-mcp")
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

db.init()

server = MCPServer("agent-memory")


def _own_session_id() -> str | None:
    name = Path.cwd().name
    return name if db.get_session(name) else None


@server.tool()
def list_sessions() -> str:
    """List every ds-agent session (id, title, provider/model, last active).
    Use this to see what other conversations/work exist before searching or
    summarizing one of them."""
    rows = db.list_sessions()
    out = [
        {
            "id": r["id"], "title": r["title"], "provider": r["provider"],
            "model": r["model"], "updated_at": r["updated_at"],
        }
        for r in rows
    ]
    return json.dumps(out)


@server.tool()
def search_other_sessions(query: str, max_sessions: int = 10) -> str:
    """Search every OTHER session's chat messages and workspace filenames
    (plots, csvs, notebooks, ...) for `query`. Use this when the user refers
    to prior work in a different conversation so you can find and reuse it
    instead of redoing it. Returns JSON: [{id, title, provider, model,
    updated_at, matches: [{role, snippet}], artifact_matches: [relative_path,
    ...]}, ...]. Follow up with get_session_summary(id) for full context."""
    own = _own_session_id()
    results = search_mod.search_sessions(query, max_sessions=max_sessions + 1)
    results = [r for r in results if r["id"] != own][:max_sessions]
    return json.dumps(results)


@server.tool()
def get_session_summary(session_id: str, max_chars: int = 6000) -> str:
    """Return another session's user/assistant messages as plain text
    (most recent last), truncated to the last `max_chars` characters.
    `session_id` may be a full id or an unambiguous prefix. Use after
    list_sessions/search_other_sessions locates the session you need more
    detail from. Does not include files — for those, pair with
    search_other_sessions' artifact_matches (paths are relative to that
    session's own workspace, not yours)."""
    row = db.get_session(session_id)
    if not row:
        candidates = [r for r in db.list_sessions() if r["id"].startswith(session_id)]
        if not candidates:
            return json.dumps({"error": f"no session matching {session_id!r}"})
        row = candidates[0]
    hist = sessions.load_history(row["id"], inline_artifacts=False)
    lines = [f"# {row['title']} ({row['id']})", ""]
    for msg in hist["messages"]:
        if msg["role"] not in ("user", "assistant"):
            continue
        content = msg["content"] if isinstance(msg["content"], str) else ""
        if content.strip():
            lines.append(f"[{msg['role']}] {content.strip()}")
    text = "\n\n".join(lines)
    if len(text) > max_chars:
        text = "…(truncated)…\n" + text[-max_chars:]
    return text


@server.tool()
def remember(text: str, tags: str = "") -> str:
    """Save a fact/preference/decision to persistent cross-session memory —
    it is shown to you (and every future session) automatically near the top
    of the system prompt. Use for durable things worth remembering (user
    preferences, project conventions, standing goals), NOT transient task
    state. `tags` is an optional comma-separated label to filter later with
    recall()."""
    text = text.strip()
    if not text:
        return json.dumps({"error": "text is empty"})
    mid = db.add_memory(text, tags.strip(), session_id=_own_session_id())
    return json.dumps({"ok": True, "id": mid})


@server.tool()
def recall(query: str = "", limit: int = 20) -> str:
    """List saved memories, most recent first, optionally filtered by
    substring `query` against the memory text or tags."""
    return json.dumps(db.list_memories(query.strip(), limit))


@server.tool()
def forget(memory_id: int) -> str:
    """Delete a memory by its numeric id (from remember()'s result or a
    recall() listing)."""
    return json.dumps({"ok": db.delete_memory(memory_id)})


def main() -> None:
    import asyncio
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
