"""Proxy MCP server sitting between the claude CLI and Kaggle's real remote
MCP endpoint (https://www.kaggle.com/mcp, reached via `npx mcp-remote`).

Why this exists: Kaggle's own tool schemas have real, non-obvious footguns
that the agent kept failing on even with explicit system-prompt guidance —
see docs/debug_notes.md (2026-09-06 kaggle entries) for the full
investigation. In order of discovery:

- `save_notebook`'s `request` fields are ALL suffixed `Nullable`
  (`newTitleNullable`, `textNullable`, ...) — plain names are silently
  accepted and ignored, not rejected, so the failure looks like an
  auth/permission problem with zero useful detail.
- `kernelTypeNullable` must be exactly `"script"` or `"notebook"` — NOT a
  language name. `languageNullable` must be `"python"`, `"r"`, or
  `"rmarkdown"`. The schema declares no `enum` for either field, so
  there's nothing in the live tool definition warning that a language name
  there is wrong.
- Once a model has gotten this wrong a few times in one conversation, a
  system-prompt fix does NOT reliably correct its next attempt — it keeps
  repeating its own earlier (wrong) tool calls regardless of what the
  prompt says. Auto-correcting here, at the point the call is actually
  forwarded, fixes it regardless of what the model asked for.

Every OTHER kaggle tool is forwarded through completely unchanged — this
is a proxy, not a reimplementation. `list_tools()` returns Kaggle's real,
live tool list verbatim (so nothing is lost if Kaggle adds/changes tools),
and `call_tool()` forwards to the same upstream tool with the same
arguments except for the specific corrections above.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import os
import sys
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

LOG = logging.getLogger("kaggle-mcp-proxy")
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

KAGGLE_TOKEN = os.environ.get("KAGGLE_MCP_TOKEN", "").strip()

# Set once, before the CLI-facing server loop starts (see _main) — every
# handler below just reads this. anyio task groups (used internally by
# stdio_client/ClientSession) must be entered and exited within the same
# stable task; establishing the connection lazily from inside a per-request
# handler task (tried first, and it broke) causes "cancel scope in a
# different task" errors once that handler task's lifetime ends while the
# connection is still supposed to be alive.
_upstream: ClientSession | None = None


async def _ensure_upstream() -> ClientSession:
    if _upstream is None:
        raise RuntimeError("kaggle upstream not connected yet")
    return _upstream


# ---- request auto-correction for known-tricky tools ----

_KERNEL_TYPES = {"script", "notebook"}
_LANGUAGES = {"python", "r", "rmarkdown"}


def _nullable_suffixed(request: dict[str, Any]) -> dict[str, Any]:
    """Rewrite plain field names to their `Nullable`-suffixed counterparts.

    `hasXNullable`/`hasX`-style boolean flags and already-suffixed keys are
    passed through unchanged; `None` values are dropped (Kaggle's schema
    treats an omitted field and an explicit null the same way).
    """
    out: dict[str, Any] = {}
    for k, v in request.items():
        if v is None:
            continue
        if k.endswith("Nullable") or k.startswith("has"):
            out[k] = v
        else:
            out[f"{k}Nullable"] = v
    return out


def _fix_save_notebook(request: dict[str, Any]) -> dict[str, Any]:
    req = _nullable_suffixed(request)
    kernel_type = req.get("kernelTypeNullable")
    language = req.get("languageNullable")
    if isinstance(kernel_type, str) and kernel_type.lower() in _LANGUAGES and kernel_type.lower() not in _KERNEL_TYPES:
        # A language name landed in kernelType (the single most common
        # mistake here) — recover it as the language if language itself
        # wasn't already set to something sensible, then fix kernelType.
        if not isinstance(language, str) or language.lower() not in _LANGUAGES:
            req["languageNullable"] = kernel_type.lower()
        req["kernelTypeNullable"] = "script"
    elif kernel_type is None:
        req["kernelTypeNullable"] = "script"
    if not isinstance(req.get("languageNullable"), str) or req["languageNullable"].lower() not in _LANGUAGES:
        req["languageNullable"] = "python"
    return req


# Tool name -> function that rewrites its `request` argument before forwarding.
_REQUEST_FIXERS = {
    "save_notebook": _fix_save_notebook,
}


async def _on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
    session = await _ensure_upstream()
    result = await session.list_tools()
    return types.ListToolsResult(tools=result.tools)


async def _on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    session = await _ensure_upstream()
    name = params.name
    arguments = dict(params.arguments or {})

    fixer = _REQUEST_FIXERS.get(name)
    if fixer and isinstance(arguments.get("request"), dict):
        before = copy.deepcopy(arguments["request"])
        arguments["request"] = fixer(arguments["request"])
        if arguments["request"] != before:
            LOG.info("auto-corrected %s request: %r -> %r", name, before, arguments["request"])

    result = await session.call_tool(name, arguments)
    return types.CallToolResult(
        content=result.content,
        structured_content=getattr(result, "structured_content", None),
        is_error=getattr(result, "is_error", False),
    )


server = Server("kaggle-proxy", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


async def _main() -> None:
    global _upstream
    if not KAGGLE_TOKEN:
        raise RuntimeError("KAGGLE_MCP_TOKEN not set — no kaggle BYOK key configured")
    params = StdioServerParameters(
        command="npx",
        args=[
            "-y", "mcp-remote", "https://www.kaggle.com/mcp",
            "--header", f"Authorization: Bearer {KAGGLE_TOKEN}",
        ],
    )
    # The upstream connection and the CLI-facing server loop share this one
    # task for their entire lifetime — required for anyio's task-group-based
    # cleanup (see the _upstream comment above).
    async with stdio_client(params) as (up_read, up_write):
        async with ClientSession(up_read, up_write) as session:
            await session.initialize()
            _upstream = session
            LOG.info("connected to upstream kaggle MCP")
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
