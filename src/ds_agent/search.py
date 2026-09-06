"""Search across every session's messages and workspace files (artifacts).

Personal single-user tool, so this is a plain substring scan over each
session's latest transcript + workspace file listing — no index to maintain.
Scans sessions newest-first and stops once `max_sessions` have matched.
"""
from __future__ import annotations
from pathlib import Path

from . import core, db, sessions

_SNIPPET_RADIUS = 80


def _snippet(text: str, at: int, needle_len: int) -> str:
    start = max(0, at - _SNIPPET_RADIUS)
    end = min(len(text), at + needle_len + _SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix


def search_sessions(query: str, max_sessions: int = 20, max_matches_per_session: int = 5) -> list[dict]:
    """Return sessions whose messages or workspace filenames match `query`.

    Each result: {id, title, provider, model, updated_at, matches: [{role, snippet}],
    artifact_matches: ["relative/path", ...]}.
    """
    q = query.strip().lower()
    if not q:
        return []

    results: list[dict] = []
    for row in db.list_sessions():
        sid = row["id"]
        matches: list[dict] = []
        try:
            hist = sessions.load_history(sid, inline_artifacts=False)
        except Exception:
            hist = {"messages": []}

        for msg in hist["messages"]:
            if len(matches) >= max_matches_per_session:
                break
            if msg["role"] not in ("user", "assistant"):
                continue
            text = msg["content"] if isinstance(msg["content"], str) else ""
            idx = text.lower().find(q)
            if idx >= 0:
                matches.append({"role": msg["role"], "snippet": _snippet(text, idx, len(q))})

        artifact_matches: list[str] = []
        workspace = Path(row["workspace"])
        if workspace.exists():
            for p in workspace.rglob("*"):
                if not p.is_file():
                    continue
                if p.name in core.WORKSPACE_SECRET_FILES:
                    continue
                if any(part in core.WORKSPACE_SECRET_DIRS for part in p.relative_to(workspace).parts[:-1]):
                    continue
                if q in p.name.lower():
                    artifact_matches.append(p.relative_to(workspace).as_posix())
                    if len(artifact_matches) >= max_matches_per_session:
                        break

        title_hit = q in (row["title"] or "").lower()
        if matches or artifact_matches or title_hit:
            results.append({
                "id": sid,
                "title": row["title"],
                "provider": row["provider"],
                "model": row["model"],
                "updated_at": row["updated_at"],
                "matches": matches,
                "artifact_matches": artifact_matches,
            })
        if len(results) >= max_sessions:
            break
    return results
