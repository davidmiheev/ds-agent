"""Export a session's messages + artifacts as a markdown transcript and a zip.

Never includes `.mcp.json` / `.claude/settings.local.json` — those hold
resolved provider API keys (see sessions._render_session_dir).
"""
from __future__ import annotations
import io
import json
import re
import zipfile
from pathlib import Path

from . import core, db, sessions
from .artifact_parser import ARTIFACT_RE

# Skip individual artifact files bigger than this, and stop adding artifacts
# once the running total crosses this — keeps the zip a sane email/Telegram
# attachment size even if the workspace has large intermediate files.
MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 300 * 1024 * 1024

_ROLE_LABEL = {
    "user": "User",
    "assistant": "Assistant",
    "thinking": "Assistant (thinking)",
    "tool-result": "Tool result",
}


def _resolve_artifact_path(path: str, workspace: Path) -> Path | None:
    p = Path(path)
    if p.exists() and p.is_file():
        return p
    cand = workspace / p.name
    if cand.exists() and cand.is_file():
        return cand
    return None


def build_markdown(sid: str) -> tuple[str, list[Path]]:
    """Render the transcript as markdown; return (markdown, artifact_paths).

    Artifact markers become a `-> artifacts/<name>` reference instead of
    being inlined, and the resolved source files are returned so the caller
    can copy them into the export zip's artifacts/ folder.
    """
    row = db.get_session(sid)
    if not row:
        raise KeyError(sid)
    workspace = Path(row["workspace"])
    hist = sessions.load_history(sid, inline_artifacts=False)

    artifacts: list[Path] = []
    seen: set[str] = set()

    def _artifact_ref(m: re.Match) -> str:
        kind, path = m.group(1), m.group(2).strip().strip("`").strip()
        resolved = _resolve_artifact_path(path, workspace)
        if resolved is None:
            return f"*[missing artifact: {path}]*"
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            artifacts.append(resolved)
        return f"*[{kind} artifact: {resolved.name}]* -> `artifacts/{resolved.name}`"

    lines = [
        f"# {row['title']}",
        "",
        f"- Session ID: `{sid}`",
        f"- Provider / model: {row['provider']} / `{row['model']}`",
        "",
        "---",
        "",
    ]
    for msg in hist["messages"]:
        role = msg["role"]
        content = msg["content"]
        if role == "tool":
            name = content.get("name", "?") if isinstance(content, dict) else "?"
            args = content.get("input") if isinstance(content, dict) else None
            lines.append(f"**Tool call: `{name}`**")
            if args:
                lines.append("```json")
                lines.append(json.dumps(args, indent=2, default=str)[:2000])
                lines.append("```")
            lines.append("")
            continue
        if not isinstance(content, str):
            content = str(content)
        content = ARTIFACT_RE.sub(_artifact_ref, content)
        lines.append(f"**{_ROLE_LABEL.get(role, role)}:**")
        lines.append("")
        lines.append(content)
        lines.append("")
    return "\n".join(lines), artifacts


def build_zip_bytes(sid: str) -> bytes:
    """Zip of messages.md + artifacts/ for one session, as in-memory bytes."""
    md, artifacts = build_markdown(sid)
    buf = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("messages.md", md)
        skipped: list[str] = []
        used_names: set[str] = set()
        for p in artifacts:
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > MAX_ARTIFACT_BYTES or total + size > MAX_TOTAL_ARTIFACT_BYTES:
                skipped.append(p.name)
                continue
            name = p.name
            n = 2
            while name in used_names:
                name = f"{p.stem}_{n}{p.suffix}"
                n += 1
            used_names.add(name)
            zf.write(p, arcname=f"artifacts/{name}")
            total += size
        if skipped:
            zf.writestr(
                "artifacts/SKIPPED.txt",
                "Too large to include in this export:\n" + "\n".join(skipped) + "\n",
            )
    return buf.getvalue()


def export_filename(sid: str) -> str:
    row = db.get_session(sid)
    title = (row or {}).get("title") or sid
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-").lower()[:40] or "session"
    return f"{slug}-{sid[:8]}.zip"
