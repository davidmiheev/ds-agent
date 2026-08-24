"""Tool result trimmer — keeps long tool outputs from blowing up the context.

When a tool result's text content is too large, we:
  1. Save the full output to <workspace>/.truncated/<tool>-<short-id>.txt
  2. Replace the model-visible text with a short head + tail + a pointer to the
     full file (so the agent can still read the full thing on demand via
     Read/Bash, without us paying the token cost every turn).

Configurable via env: TOOL_RESULT_MAX_BYTES (default 30KB), TOOL_RESULT_KEEP_HEAD
(default 8KB), TOOL_RESULT_KEEP_TAIL (default 4KB).
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
from typing import Any

MAX_BYTES = int(os.environ.get("TOOL_RESULT_MAX_BYTES", str(30 * 1024)))
KEEP_HEAD = int(os.environ.get("TOOL_RESULT_KEEP_HEAD", str(8 * 1024)))
KEEP_TAIL = int(os.environ.get("TOOL_RESULT_KEEP_TAIL", str(4 * 1024)))


def trim_text(text: str, *, workspace: Path, tool_name: str) -> tuple[str, bool]:
    """If `text` exceeds MAX_BYTES, save full output to disk and return a
    truncated version with a pointer to the full file. Returns (text, was_trimmed)."""
    if not isinstance(text, str):
        return text, False
    if len(text) <= MAX_BYTES:
        return text, False

    h = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:10]
    safe_tool = (tool_name or "tool").replace("/", "_").replace(" ", "_")[:30]
    out_dir = workspace / ".truncated"
    out_dir.mkdir(parents=True, exist_ok=True)
    full = out_dir / f"{safe_tool}-{h}.txt"
    if not full.exists():
        full.write_text(text, encoding="utf-8", errors="replace")

    head = text[:KEEP_HEAD]
    tail = text[-KEEP_TAIL:] if len(text) > KEEP_TAIL else ""
    mid_msg = (
        f"\n\n... [TRUNCATED — {len(text) - KEEP_HEAD - KEEP_TAIL:,} bytes omitted, "
        f"full output saved to {full}] ...\n\n"
    )
    return head + mid_msg + tail, True


def trim_tool_result_blocks(blocks: Any, *, workspace: Path, tool_name: str) -> tuple[Any, int]:
    """Walk a tool_result content (string OR list of content blocks) and trim
    oversized text portions. Returns (new_blocks, n_trimmed)."""
    n = 0
    if isinstance(blocks, str):
        new, trimmed = trim_text(blocks, workspace=workspace, tool_name=tool_name)
        return new, (1 if trimmed else 0)
    if not isinstance(blocks, list):
        return blocks, 0
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            new, trimmed = trim_text(b.get("text", ""), workspace=workspace, tool_name=tool_name)
            if trimmed:
                b["text"] = new
                n += 1
    return blocks, n
