"""Parse __ARTIFACT__ markers in agent stdout so plots / files / CSVs surface as
proper content blocks in the chat UI instead of a path buried in text.

Marker format the agent is told to emit:
    __ARTIFACT__:plot:/absolute/path/to/plot.png
    __ARTIFACT__:csv:/absolute/path/to/data.csv
    __ARTIFACT__:text:/absolute/path/to/notes.md

The marker line is replaced with a short note; the file is read once and its
bytes encoded as base64 for inline image rendering or downloadable file blocks.
"""
from __future__ import annotations
import base64
import mimetypes
import re
from pathlib import Path

ARTIFACT_RE = re.compile(r"^__ARTIFACT__:([\w-]+):(.+?)\s*$", re.MULTILINE)

# 5 MB hard cap; refuse to inline bigger files.
MAX_INLINE_BYTES = 5 * 1024 * 1024

_MIME = {
    "plot":  "image/png",
    "png":   "image/png",
    "jpg":   "image/jpeg",
    "jpeg":  "image/jpeg",
    "svg":   "image/svg+xml",
    "pdf":   "application/pdf",
    "csv":   "text/csv",
    "json":  "application/json",
    "text":  "text/plain",
    "md":    "text/markdown",
    "html":  "text/html",
    "ipynb": "application/x-ipynb+json",
}


def rewrite_tool_result_content(text: str) -> str:
    """Return a HTML-annotated version of `text` with __ARTIFACT__ markers replaced.

    The browser parses the surrounding HTML to extract embedded artifacts; see
    static/app.js. We do the heavy lifting server-side so the JS stays tiny.
    """
    if not text or "__ARTIFACT__" not in text:
        return text

    def _repl(m: re.Match) -> str:
        kind, path = m.group(1), m.group(2).strip()
        p = Path(path)
        if not p.exists() or not p.is_file():
            return f"\n[artifact missing: {path}]\n"
        try:
            data = p.read_bytes()
        except Exception as e:
            return f"\n[artifact read error: {e}]\n"
        if len(data) > MAX_INLINE_BYTES:
            return f"\n[artifact too large: {path} ({len(data)} bytes)]\n"
        mime = _MIME.get(kind) or (mimetypes.guess_type(path)[0] or "application/octet-stream")
        b64 = base64.b64encode(data).decode()
        return (
            f"\n<div class=\"artifact\" "
            f"data-kind=\"{kind}\" data-mime=\"{mime}\" data-name=\"{p.name}\" "
            f"data-path=\"{path}\" data-b64=\"{b64}\"></div>\n"
        )

    return ARTIFACT_RE.sub(_repl, text)
