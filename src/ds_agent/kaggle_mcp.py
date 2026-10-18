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
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

LOG = logging.getLogger("kaggle-mcp-proxy")
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

KAGGLE_TOKEN = os.environ.get("KAGGLE_MCP_TOKEN", "").strip()

# Env vars `npx mcp-remote` needs to reach kaggle.com from behind an HTTP(S)
# proxy. With no explicit `env`, the mcp SDK hands the child process only its
# small "safe" default set (HOME, PATH, ...), dropping all of these — behind a
# TLS-intercepting proxy (sandboxes, corporate networks) npm then fails with
# SELF_SIGNED_CERT_IN_CHAIN and the proxy dies before `initialize`, which the
# claude CLI only reports as a connect timeout. See docs/debug_notes.md.
_UPSTREAM_PASSTHROUGH_ENV = (
    "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
    "NODE_EXTRA_CA_CERTS", "NODE_USE_ENV_PROXY", "SSL_CERT_FILE",
    "npm_config_cafile", "npm_config_registry",
)


def _upstream_env() -> dict[str, str]:
    env = get_default_environment()
    env.update({k: os.environ[k] for k in _UPSTREAM_PASSTHROUGH_ENV if k in os.environ})
    return env


def _upstream_params(token: str) -> StdioServerParameters:
    """How the `npx mcp-remote` bridge to Kaggle is started.

    `--disable-cookies` is what makes the bearer token count. Kaggle's
    `initialize` response sets an anonymous web-session cookie
    (`ka_sessionid`); mcp-remote 0.14 stores cookies a server sets and replays
    them on every later request, and Kaggle then authenticates by that
    anonymous session instead of the `Authorization` header -- so every
    account-scoped tool (quota, notebooks, own datasets) answered
    "Unauthenticated" while public ones kept working. `@latest` rather than a
    bare name so `npx` resolves the newest release instead of reusing an older
    cached copy. See docs/debug_notes.md (2026-09-27).
    """
    return StdioServerParameters(
        command="npx",
        args=[
            "-y", "mcp-remote@latest", "https://www.kaggle.com/mcp",
            "--header", f"Authorization: Bearer {token}",
            "--disable-cookies",
        ],
        env=_upstream_env(),
    )


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
# The fixers live in kaggle_requests.py (shared with the batch runner's
# command-line clients, which run without the `mcp` package); the names are
# re-exported here for existing imports.
try:  # package import (ds-agent: python -m ds_agent.kaggle_mcp)
    from . import kaggle_batch
    from .kaggle_requests import _REQUEST_FIXERS, _fix_save_notebook, _nullable_suffixed
except ImportError:  # flat import (a vendored copy run as a script)
    import kaggle_batch  # type: ignore[no-redef]
    from kaggle_requests import _REQUEST_FIXERS, _fix_save_notebook, _nullable_suffixed  # type: ignore[no-redef]

__all__ = ["_REQUEST_FIXERS", "_fix_save_notebook", "_nullable_suffixed", "kaggle_batch"]


async def _on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
    session = await _ensure_upstream()
    result = await session.list_tools()
    # Kaggle's live tools verbatim, plus the local batch runner's (kaggle_batch.py).
    local = [types.Tool(name=t["name"], description=t["description"], input_schema=t["input_schema"])
             for t in kaggle_batch.TOOLS]
    return types.ListToolsResult(tools=list(result.tools) + local)


def _text_result(payload: Any, is_error: bool = False) -> types.CallToolResult:
    import json

    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, indent=1, default=str))],
        is_error=is_error,
    )


async def _on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    session = await _ensure_upstream()
    name = params.name
    arguments = dict(params.arguments or {})

    if name in kaggle_batch.TOOL_NAMES:  # local tools: the batch runner, on top of upstream calls
        try:
            return _text_result(await kaggle_batch.dispatch(name, arguments, session.call_tool))
        except (kaggle_batch.BatchError, kaggle_batch.KaggleToolError, KeyError) as exc:
            return _text_result({"error": f"{type(exc).__name__}: {exc}"}, is_error=True)

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
    params = _upstream_params(KAGGLE_TOKEN)
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
