"""Thin MCP wrapper around google-colab-cli for programmatic Colab access.

Exposes a small set of MCP tools to the agent (claude code etc.):

    colab_new        — provision a runtime (CPU / T4 / L4 / A100 / TPU)
    colab_execute    — run code, get stdout/stderr/images back as MCP content
    colab_upload     — upload a local file onto the runtime's /content dir
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
import posixpath
import sys
import time
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

# Strip inherited proxy env vars. The parent shell may export a dead SOCKS
# proxy (ALL_PROXY=socks5://127.0.0.1:1080) which makes requests raise
# InvalidSchema (no socksio installed) on every Google API call.
for _k in list(os.environ):
    if _k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        del os.environ[_k]

# Resolve ~/.config/colab-cli (the standard location the CLI uses) so we
# share auth tokens and session metadata with `colab` CLI.
os.environ.setdefault("HOME", str(Path.home()))
os.makedirs(os.path.dirname(TOKEN_CONFIG_PATH), exist_ok=True)

LOG = logging.getLogger("colab-mcp")
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

# Runtime proxy tokens (the "colab-runtime-proxy-token" used to authenticate
# the kernel websocket) are short-lived — observed to expire at exactly
# 3600s, independent of how long the underlying runtime itself stays up.
# Refresh a bit before the deadline so an execute call never races expiry.
_TOKEN_REFRESH_MARGIN_S = 60

# The pending OAuth flow's PKCE verifier is also persisted here (0600) so a
# `colab_auth(code=...)` still completes if the MCP server process restarted
# after `colab_auth()` handed out the URL — the claude CLI can and does restart
# MCP servers between turns, which previously forced the user to redo the whole
# browser sign-in. Google authorization codes expire after ~10 minutes, so an
# older pending flow is useless and is discarded.
PENDING_AUTH_PATH = os.path.join(os.path.dirname(TOKEN_CONFIG_PATH), "pending_auth.json")
_PENDING_AUTH_TTL_S = 15 * 60
_OAUTH_REDIRECT_URI = "https://sdk.cloud.google.com/applicationdefaultauthcode.html"

# Per-MCP-process state. We could share a StateStore with the CLI but
# process isolation keeps things simple.
_state = {
    "creds": None,
    "client": None,
    "store": StateStore(),
    "active_session": None,  # SessionState
    "runtime": None,          # ColabRuntime
    "pending_auth_url": None,
    "_auth_flow": None,          # the in-flight OAuth Flow (holds the PKCE code_verifier)
    "notebook_hash": None,       # uuid.UUID used for the active session's assign() calls
    "runtime_token_expires_at": None,  # time.time() deadline for the cached proxy token
    "variant": None,             # Variant enum for the active session (for token refresh)
    "accelerator": None,         # Accelerator enum for the active session
    "shape": None,               # Shape enum for the active session
}

# ---------------- helpers ----------------

def _get_creds():
    if _state["creds"] is not None:
        return _state["creds"]
    # Non-interactive load: this runs as an MCP subprocess whose stdin is the
    # MCP pipe, so we must NEVER fall into colab_cli's _run_remote_flow (it
    # calls input() and blocks forever). Load the token file directly and
    # refresh it; if that fails, raise so the caller surfaces auth_required.
    creds = None
    if os.path.exists(TOKEN_CONFIG_PATH):
        try:
            from google.oauth2.credentials import Credentials
            creds = Credentials.from_authorized_user_file(TOKEN_CONFIG_PATH, PUBLIC_SCOPES)
        except Exception as e:
            LOG.warning("failed to load token from %s: %s", TOKEN_CONFIG_PATH, e)
    if creds is not None and not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                from google.auth.transport.requests import Request
                creds.refresh(Request())
                # persist the refreshed token
                try:
                    with open(TOKEN_CONFIG_PATH, "w") as f:
                        f.write(creds.to_json())
                except Exception:
                    pass
            except Exception as e:
                LOG.warning("token refresh failed: %s", e)
                creds = None
        else:
            creds = None
    if creds is None or not creds.valid:
        raise RuntimeError(
            "No valid Colab credentials. Run src/colab_mcp/auth_once.py once, "
            "or call `colab_auth` with an authorization code."
        )
    _state["creds"] = creds
    return creds

def _get_client():
    if _state["client"] is not None:
        return _state["client"]
    creds = _get_creds()
    # Client expects a session with .request() — wrap raw Credentials in an
    # AuthorizedSession (matches colab_cli.auth.get_credentials()).
    from google.auth.transport.requests import AuthorizedSession
    _state["client"] = Client(Prod(), AuthorizedSession(creds))
    return _state["client"]

def _active_session() -> Optional[SessionState]:
    if _state["active_session"] is not None:
        return _state["active_session"]
    sessions = _state["store"].list()
    if not sessions:
        return None
    # Pick the most recently used
    return list(sessions.values())[-1]

def _refresh_runtime_token_if_needed() -> None:
    """Re-issue the runtime proxy token if it's expired or about to be.

    The proxy token is short-lived (see `_TOKEN_REFRESH_MARGIN_S`) and
    unrelated to the runtime's actual lifetime — any agent session running
    longer than ~an hour will outlive it. `client.assign()` called again
    with the SAME notebook_hash returns the existing, still-live assignment
    with a freshly issued token (not a new billable runtime) as long as the
    runtime is still up server-side, so this is a reconnect, not a
    recreate. Deliberately does NOT prune/remove the session on an expired
    token — an expired *token* does not mean a dead *runtime*, and treating
    it that way would orphan a live, still-billing runtime instead of just
    reconnecting to it.
    """
    s = _state["active_session"]
    if s is None:
        return
    expires_at = _state.get("runtime_token_expires_at")
    if expires_at is not None and time.time() < expires_at - _TOKEN_REFRESH_MARGIN_S:
        return  # still fresh
    notebook_hash = _state.get("notebook_hash") or uuid.uuid4()
    res = _get_client().assign(
        notebook_hash,
        variant=_state.get("variant"),
        accelerator=_state.get("accelerator"),
        shape=_state.get("shape"),
    )
    s.token = res.runtime_proxy_info.token
    s.url = res.runtime_proxy_info.url
    _state["store"].add(s)
    _state["notebook_hash"] = notebook_hash
    _state["runtime_token_expires_at"] = time.time() + res.runtime_proxy_info.token_expires_in_seconds
    # Force a rebuild with the fresh token below — but keep kernel_id/
    # session_id on `s` so _active_runtime() reconnects to the SAME running
    # kernel instead of starting a new one.
    _state["runtime"] = None


def _active_runtime() -> ColabRuntime:
    s = _state["active_session"]
    if s is None:
        raise RuntimeError("No active session. Call `colab_new` first.")
    _refresh_runtime_token_if_needed()
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
        # The Colab Contents API (used by colab_upload) is always rooted at
        # /content regardless of the kernel's actual cwd — without this, a
        # file uploaded to contents-path "foo.csv" can silently fail to
        # `open()` from colab_execute'd code if the kernel's cwd isn't
        # /content. The official CLI does this same chdir before every
        # exec/repl; here it only needs to happen once per fresh kernel.
        _state["runtime"].execute_code(
            "import os; os.makedirs('/content', exist_ok=True); os.chdir('/content')"
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
            name="colab_upload",
            description=(
                "Upload a local file to the active Colab runtime so "
                "`colab_execute` code can open it. Always lands under "
                "/content/ on the runtime (the runtime's kernel cwd is "
                "chdir'd to /content) — pass a bare filename or relative "
                "sub-path for `remote_path` (missing sub-directories are "
                "created), and reference that exact same relative path with "
                "open(...) from colab_execute code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "local_path": {"type": "string", "description": "Path to the local file to upload"},
                    "remote_path": {
                        "type": "string",
                        "description": "Destination path on the runtime, relative to /content (defaults to the local file's basename)",
                    },
                },
                "required": ["local_path"],
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

def _content_relative_path(remote_path: str) -> str:
    """Normalize a user-supplied upload path to one relative to /content.

    A leading `/` or `/content/` is accepted and stripped; anything that would
    resolve outside /content (e.g. `../etc/x`) is rejected.
    """
    rel = posixpath.normpath(remote_path.strip().lstrip("/"))
    if rel == "content" or rel.startswith("content/"):
        rel = rel[len("content"):].lstrip("/")
    if rel in ("", ".") or rel == ".." or rel.startswith("../"):
        raise ValueError(f"remote_path must name a file under /content, got {remote_path!r}")
    return rel


def _ensure_remote_dirs(contents, api_dir: str) -> None:
    """Create each missing directory along `api_dir` via the Contents API.

    PUT with type=directory is idempotent in Jupyter's contents API (an
    existing directory is left as-is), so no existence check is needed.
    """
    parts = [p for p in api_dir.split("/") if p]
    for i in range(1, len(parts) + 1):
        contents._request("PUT", "/".join(parts[:i]), json_data={"type": "directory"})


def _oauth_client_config() -> dict:
    from importlib import resources
    config_resource = resources.files("colab_cli").joinpath("oauth_config.json")
    return json.loads(config_resource.read_text())


def _new_flow(code_verifier: Optional[str] = None):
    from colab_cli.auth import PUBLIC_SCOPES
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_config(
        _oauth_client_config(), PUBLIC_SCOPES, code_verifier=code_verifier,
    )
    flow.redirect_uri = _OAUTH_REDIRECT_URI
    return flow


def _save_pending_auth(code_verifier: str) -> None:
    fd = os.open(PENDING_AUTH_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"code_verifier": code_verifier, "created_at": time.time()}, f)


def _load_pending_auth() -> Optional[str]:
    """Return a still-fresh persisted PKCE verifier, or None."""
    try:
        with open(PENDING_AUTH_PATH) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if time.time() - float(data.get("created_at", 0)) > _PENDING_AUTH_TTL_S:
        _clear_pending_auth()
        return None
    return data.get("code_verifier") or None


def _clear_pending_auth() -> None:
    try:
        os.remove(PENDING_AUTH_PATH)
    except FileNotFoundError:
        pass


def _start_oauth() -> str:
    """Build the OAuth URL without consuming input. Returns the URL."""
    # authorization_url() auto-generates flow.code_verifier and embeds a
    # code_challenge (PKCE) derived from it into the returned URL.
    flow = _new_flow()
    auth_url, _ = flow.authorization_url(prompt="consent", token_usage="remote")
    _state["pending_auth_url"] = auth_url
    # Persist the SAME Flow object — not just the client config — so
    # _complete_oauth() reuses its code_verifier. A fresh Flow built from
    # the config alone would autogenerate a *different* random verifier
    # that fetch_token() would send instead, which Google's token endpoint
    # rejects (invalid_grant) since it no longer matches the code_challenge
    # already sent above. See docs/debug_notes.md.
    _state["_auth_flow"] = flow
    # ...and the verifier on disk, so the flow survives a server restart.
    _save_pending_auth(flow.code_verifier)
    return auth_url


def _complete_oauth(code: str) -> dict:
    flow = _state.get("_auth_flow")
    if flow is None:
        # Server restarted since colab_auth() handed out the URL: rebuild the
        # flow around the persisted verifier (it must match the code_challenge
        # in that URL, or Google rejects the code with invalid_grant).
        verifier = _load_pending_auth()
        if verifier:
            flow = _new_flow(code_verifier=verifier)
    if flow is None:
        raise RuntimeError(
            "No pending OAuth flow (none started, or it is older than "
            f"{_PENDING_AUTH_TTL_S // 60} minutes). Call colab_auth with no "
            "arguments to get a fresh authorization URL, then retry "
            "colab_auth(code=...)."
        )
    flow.fetch_token(code=code)
    creds = flow.credentials
    # persist (matches the official CLI)
    with open(TOKEN_CONFIG_PATH, "w") as f:
        f.write(creds.to_json())
    _state["creds"] = creds
    from google.auth.transport.requests import AuthorizedSession
    _state["client"] = Client(Prod(), AuthorizedSession(creds))
    _state["pending_auth_url"] = None
    _state["_auth_flow"] = None
    _clear_pending_auth()
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
            notebook_hash = uuid.uuid4()
            res = client.assign(notebook_hash, variant=variant, accelerator=accel, shape=shape)
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
            # Remembered so a later expired-token refresh can call assign()
            # again and reconnect to this SAME runtime instead of either
            # failing outright or accidentally provisioning a new one.
            _state["notebook_hash"] = notebook_hash
            _state["variant"] = variant
            _state["accelerator"] = accel
            _state["shape"] = shape
            _state["runtime_token_expires_at"] = time.time() + res.runtime_proxy_info.token_expires_in_seconds
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

        if name == "colab_upload":
            from colab_cli.contents import ContentsClient
            local_path = arguments["local_path"]
            if not os.path.isfile(local_path):
                return [types.TextContent(type="text", text=json.dumps(
                    {"error": f"local file not found: {local_path}"}
                ))]
            try:
                remote_path = _content_relative_path(
                    arguments.get("remote_path") or os.path.basename(local_path)
                )
            except ValueError as e:
                return [types.TextContent(type="text", text=json.dumps({"error": str(e)}))]
            # Ensures a session/runtime exists, the proxy token is fresh, and
            # the kernel's cwd is /content.
            _active_runtime()
            contents = ContentsClient(_state["active_session"])
            # The Contents API is rooted at the Jupyter server root, which on
            # Colab is `/` — NOT /content (the official CLI's own `install -r`
            # uploads to "content/<name>" and then reads "/content/<name>").
            # So prefix "content/", and create missing parent dirs first: a PUT
            # to a path whose parent doesn't exist fails with a bare HTTP 500.
            api_path = f"content/{remote_path}"
            _ensure_remote_dirs(contents, posixpath.dirname(api_path))
            contents.upload(local_path, api_path)
            return [types.TextContent(type="text", text=json.dumps({
                "status": "uploaded",
                "remote_path": remote_path,
                "note": f"In colab_execute code: open({remote_path!r}) — runtime cwd is /content.",
            }))]

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
            _state["notebook_hash"] = None
            _state["variant"] = None
            _state["accelerator"] = None
            _state["shape"] = None
            _state["runtime_token_expires_at"] = None
            return [types.TextContent(type="text", text=json.dumps({
                "status": "stopped", "session": s.name
            }))]

        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        # If it's a credential error, hint at the auth flow. Don't mistake
        # programming bugs (AttributeError etc.) for auth failures.
        msg = str(e)
        is_auth_err = (
            "has no attribute" not in msg
            and ("no colab credentials" in msg.lower()
                 or "oauth" in msg.lower()
                 or "invalid_grant" in msg
                 or "token" in msg.lower() and "expired" in msg.lower())
        )
        if is_auth_err:
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
