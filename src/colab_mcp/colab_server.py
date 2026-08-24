"""Thin MCP wrapper around google-colab-cli for programmatic Colab access.

Exposes a small set of MCP tools to the agent (claude code etc.):

    colab_new        — provision a runtime (CPU / T4 / L4 / A100 / TPU)
    colab_execute    — run code, get stdout/stderr/images back as MCP content
    colab_status     — show runtime info
    colab_stop       — release the runtime
    colab_sessions   — list active runtimes
    colab_install    — install Python packages on the runtime
    colab_auth       — paste the OAuth authorization code (one-time setup)

Auth model: reuse the public OAuth client that ships inside
google-colab-cli (no user setup of GCP project / OAuth client required).
On first use the server prints an auth URL to its stderr; the user visits
it in any browser, copies the code, and calls `colab_auth(code=...)`. The
resulting tokens are stored at `~/.config/colab-cli/token.json` and reused.
"""
from __future__ import annotations
import asyncio
import base64
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

# Use the stdio MCP server primitives from the official SDK.
from mcp.server import Server, NotificationOptions
from mcp.server.stdio import stdio_server
from mcp import types

# colab_cli internals — we reuse the same Client + auth + state the official
# CLI uses, so we benefit from its keep-alive, error handling, etc.
from colab_cli.client import Client, Prod, Accelerator, Variant, Shape
from colab_cli.state import StateStore, SessionState
from colab_cli.auth import (
    AuthProvider, get_credentials, _get_google_auth_credentials,
    TOKEN_CONFIG_PATH, PUBLIC_SCOPES,
)
from colab_cli.runtime import ColabRuntime

# Resolve ~/.config/colab-cli (the standard location the CLI uses) so we
# share auth tokens and session metadata with `colab` CLI.
os.environ.setdefault("HOME", str(Path.home()))
os.makedirs(os.path.dirname(TOKEN_CONFIG_PATH), exist_ok=True)

LOG = logging.getLogger("colab-mcp")
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

# Per-MCP-process state. We could share a StateStore with the CLI but
# process isolation keeps things simple.
_state = {
    "creds": None,
    "client": None,
    "store": StateStore(),
    "active_session": None,  # SessionState
    "runtime": None,          # ColabRuntime
    "pending_auth_url": None,
}

# ---------------- helpers ----------------

def _get_creds():
    if _state["creds"] is not None:
        return _state["creds"]
    # Try the public client shipped inside google-colab-cli first.
    creds = _get_google_auth_credentials("")  # "" path falls back to inlined config
    if creds is None:
        raise RuntimeError(
            "No Colab credentials found. Call `colab_auth` to start the OAuth flow."
        )
    _state["creds"] = creds
    return creds

def _get_client():
    if _state["client"] is not None:
        return _state["client"]
    creds = _get_creds()
    _state["client"] = Client(Prod(), creds)
    return _state["client"]

def _active_session() -> Optional[SessionState]:
    if _state["active_session"] is not None:
        return _state["active_session"]
    sessions = _state["store"].list()
    if not sessions:
        return None
    # Pick the most recently used
    return list(sessions.values())[-1]

def _active_runtime() -> ColabRuntime:
    s = _state["active_session"]
    if s is None:
        raise RuntimeError("No active session. Call `colab_new` first.")
    if _state["runtime"] is None:
        def on_kid(kid):
            s.kernel_id = kid
            _state["store"].add(s)
        def on_sid(sid):
            s.session_id = sid
            _state["store"].add(s)
        _state["runtime"] = ColabRuntime(
            s.url, s.token,
            kernel_id=s.kernel_id, session_id=s.session_id,
            on_kernel_started=on_kid, on_session_started=on_sid,
        )
    return _state["runtime"]

# ---------------- MCP tool definitions ----------------

server = Server("colab-mcp")

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="colab_auth",
            description=(
                "Start or complete the Colab OAuth flow. With no arguments, "
                "prints an authorization URL to the server stderr. Pass "
                "`code=<authorization-code>` to complete the flow. "
                "Tokens are cached at ~/.config/colab-cli/token.json."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Authorization code from Google"},
                },
            },
        ),
        types.Tool(
            name="colab_new",
            description=(
                "Provision a new Colab runtime. Returns the runtime URL and "
                "session name. Specify `gpu` (T4/L4/G4/A100/H100), `tpu` "
                "(v5e1/v6e1), or omit for CPU. `high_mem=true` requests a high-RAM "
                "shape (Pro+ only). Triggers OAuth on first use."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Optional session name"},
                    "gpu": {"type": "string", "enum": ["T4", "L4", "G4", "A100", "H100"]},
                    "tpu": {"type": "string", "enum": ["v5e1", "v6e1"]},
                    "high_mem": {"type": "boolean", "default": False},
                },
            },
        ),
        types.Tool(
            name="colab_status",
            description="Show info about the active session (name, endpoint, accelerator, shape, status).",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="colab_execute",
            description=(
                "Execute Python code on the active Colab runtime. Returns "
                "stdout, stderr, and image outputs (matplotlib plots etc.) "
                "as MCP content blocks. Use a Python heredoc for multi-line code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source to run"},
                    "timeout": {"type": "number", "default": 120, "description": "Wall-clock seconds"},
                },
                "required": ["code"],
            },
        ),
        types.Tool(
            name="colab_stop",
            description="Release the active Colab runtime.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="colab_sessions",
            description="List all active Colab runtimes on the user's account.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="colab_install",
            description="Install Python packages on the active Colab runtime via uv (falls back to pip).",
            inputSchema={
                "type": "object",
                "properties": {
                    "packages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": 'Package specs, e.g. ["torch", "transformers[torch]"]',
                    },
                },
                "required": ["packages"],
            },
        ),
    ]


# ---------------- tool implementations ----------------

def _start_oauth() -> str:
    """Build the OAuth URL without consuming input. Returns the URL."""
    from colab_cli.auth import _run_remote_flow, PUBLIC_SCOPES
    from importlib import resources
    config_resource = resources.files("colab_cli").joinpath("oauth_config.json")
    client_config = json.loads(config_resource.read_text())

    # Reproduce InstalledAppFlow + remote-redirect URL build, but don't run
    # the blocking fetch_token.
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_config(client_config, PUBLIC_SCOPES)
    flow.redirect_uri = "https://sdk.cloud.google.com/applicationdefaultauthcode.html"
    auth_url, _ = flow.authorization_url(prompt="consent", token_usage="remote")
    _state["pending_auth_url"] = auth_url
    # also save the client config & scopes so colab_auth(code=...) can complete
    _state["_auth_client_config"] = client_config
    return auth_url


def _complete_oauth(code: str) -> dict:
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_config(
        _state["_auth_client_config"], PUBLIC_SCOPES
    )
    flow.redirect_uri = "https://sdk.cloud.google.com/applicationdefaultauthcode.html"
    flow.fetch_token(code=code)
    creds = flow.credentials
    # persist (matches the official CLI)
    with open(TOKEN_CONFIG_PATH, "w") as f:
        f.write(creds.to_json())
    _state["creds"] = creds
    _state["client"] = Client(Prod(), creds)
    _state["pending_auth_url"] = None
    return {"status": "authenticated"}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.ContentBlock]:
    try:
        if name == "colab_auth":
            if "code" not in arguments:
                url = _start_oauth()
                # also print to stderr so the hosting process / user sees it
                sys.stderr.write(f"\n[colab-mcp] To authorize, visit:\n  {url}\n\n")
                sys.stderr.flush()
                return [types.TextContent(
                    type="text",
                    text=json.dumps({
                        "status": "auth_required",
                        "auth_url": url,
                        "instructions": (
                            "Open the URL above in any browser, sign in to your "
                            "Google account, and you'll see an authorization code. "
                            "Then call colab_auth again with code=<that-code>."
                        ),
                    }, indent=2),
                )]
            res = _complete_oauth(arguments["code"])
            return [types.TextContent(type="text", text=json.dumps(res))]

        if name == "colab_new":
            client = _get_client()
            session_name = arguments.get("session") or uuid.uuid4().hex[:6]
            gpu = arguments.get("gpu")
            tpu = arguments.get("tpu")
            high_mem = arguments.get("high_mem", False)

            if tpu:
                variant = Variant.TPU
                accel = Accelerator.V5E1 if tpu.lower() == "v5e1" else Accelerator.V6E1
            elif gpu:
                variant = Variant.GPU
                accel = {"a100": Accelerator.A100, "h100": Accelerator.H100,
                         "l4": Accelerator.L4, "t4": Accelerator.T4,
                         "g4": Accelerator.G4}.get(gpu.lower(), Accelerator.A100)
            else:
                variant = Variant.DEFAULT
                accel = Accelerator.NONE

            from colab_cli.client import resolve_assign_shape
            shape = resolve_assign_shape(accel, high_mem=high_mem)
            res = client.assign(uuid.uuid4(), variant=variant, accelerator=accel, shape=shape)
            token = res.runtime_proxy_info.token
            url = res.runtime_proxy_info.url
            endpoint = res.endpoint
            s = SessionState(
                name=session_name, token=token, url=url, endpoint=endpoint,
                variant=variant.value, accelerator=accel.value,
                machine_shape=("HIGH_RAM" if shape == Shape.HIGH_RAM else "STANDARD"),
            )
            _state["store"].add(s)
            _state["active_session"] = s
            _state["runtime"] = None  # lazy init
            return [types.TextContent(
                type="text",
                text=json.dumps({
                    "status": "provisioning",
                    "session": session_name,
                    "endpoint": endpoint,
                    "accelerator": accel.value,
                    "shape": s.machine_shape,
                    "note": "The kernel will start on the first `colab_execute` call. Provisioning can take ~10-30s.",
                }, indent=2),
            )]

        if name == "colab_status":
            s = _active_session()
            if s is None:
                return [types.TextContent(type="text", text="No active session.")]
            return [types.TextContent(type="text", text=json.dumps({
                "session": s.name,
                "endpoint": s.endpoint,
                "accelerator": s.accelerator,
                "variant": s.variant,
                "machine_shape": s.machine_shape,
                "kernel_id": s.kernel_id,
                "session_id": s.session_id,
            }, indent=2))]

        if name == "colab_sessions":
            assignments = _get_client().list_assignments()
            out = []
            for a in assignments:
                out.append({
                    "endpoint": a.endpoint,
                    "accelerator": getattr(a, "accelerator", "?"),
                    "variant": getattr(a, "variant", "?"),
                    "shape": getattr(a, "shape", "?"),
                })
            return [types.TextContent(type="text", text=json.dumps({"sessions": out}, indent=2))]

        if name == "colab_execute":
            code = arguments["code"]
            timeout = float(arguments.get("timeout", 120))
            runtime = _active_runtime()
            outputs = runtime.execute_code(code, timeout=timeout)
            blocks: list[types.ContentBlock] = []
            for out in outputs:
                ot = out.get("output_type")
                if ot == "stream":
                    blocks.append(types.TextContent(
                        type="text",
                        text=out.get("text", ""),
                    ))
                elif "data" in out:
                    data = out["data"]
                    if "text/plain" in data:
                        blocks.append(types.TextContent(type="text", text=data["text/plain"]))
                    for mime in ("image/png", "image/jpeg", "image/svg+xml"):
                        if mime in data:
                            blocks.append(types.ImageContent(
                                type="image",
                                mime_type=mime,
                                data=data[mime],  # already base64 per Jupyter spec
                            ))
                elif ot == "error":
                    tb = "\n".join(out.get("traceback", [])) or f"{out.get('ename')}: {out.get('evalue')}"
                    blocks.append(types.TextContent(type="text", text=f"[error]\n{tb}"))
            if not blocks:
                blocks.append(types.TextContent(type="text", text="(no output)"))
            return blocks

        if name == "colab_install":
            pkgs = arguments["packages"]
            pkgs_src = " ".join(pkgs)
            code = (
                "import subprocess, sys\n"
                f"r = subprocess.run([sys.executable, '-m', 'pip', 'install'] + {pkgs!r}, "
                "capture_output=True, text=True)\n"
                "print(r.stdout[-2000:])\n"
                "if r.returncode != 0:\n"
                "    print('STDERR:', r.stderr[-2000:], file=sys.stderr)\n"
                "    raise SystemExit(r.returncode)\n"
            )
            return await call_tool("colab_execute", {"code": code, "timeout": 300})

        if name == "colab_stop":
            s = _active_session()
            if s is None:
                return [types.TextContent(type="text", text="No active session.")]
            try:
                if _state["runtime"]:
                    try:
                        _state["runtime"].kernel_client.stop()
                    except Exception:
                        pass
                _state["store"].remove(s.name)
            except Exception as e:
                return [types.TextContent(type="text", text=f"Error stopping: {e}")]
            _state["active_session"] = None
            _state["runtime"] = None
            return [types.TextContent(type="text", text=json.dumps({
                "status": "stopped", "session": s.name
            }))]

        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        # If it's a credential error, hint at the auth flow.
        msg = str(e)
        if "credentials" in msg.lower() or "oauth" in msg.lower() or "No Colab credentials" in msg:
            try:
                url = _start_oauth()
                sys.stderr.write(f"\n[colab-mcp] Auth needed. Visit:\n  {url}\n")
                sys.stderr.flush()
                return [types.TextContent(
                    type="text",
                    text=json.dumps({
                        "error": "auth_required",
                        "message": msg,
                        "auth_url": url,
                    }, indent=2),
                )]
            except Exception:
                pass
        return [types.TextContent(type="text", text=f"Error: {e}\n\n{tb}")]


async def main():
    LOG.info("colab-mcp starting")
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
