"""FastAPI entrypoint: UI pages, REST routes, WebSocket bridge to the agent."""
from __future__ import annotations
import asyncio
import json
import time
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Form, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import core, db, crypto, sessions, telegram
from .providers import env_for

db.init()

app = FastAPI(title="Coding Agent")

_telegram_task: asyncio.Task | None = None


@app.on_event("startup")
async def _startup_tasks() -> None:
    """Seed BYOK keys and launch background workers."""
    import os
    or_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if or_key and not crypto.load_key("openrouter"):
        crypto.save_key("openrouter", or_key, "https://openrouter.ai/api", "OpenRouter (env)")

    ant_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if ant_key and not crypto.load_key("anthropic"):
        crypto.save_key("anthropic", ant_key, label="Anthropic (env)")

    mm_key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if mm_key and not crypto.load_key("minimax"):
        crypto.save_key("minimax", mm_key, "https://api.minimaxi.chat/v1", "MiniMax (env)")

    kg_key = (
        os.environ.get("KAGGLE_API_TOKEN")
        or os.environ.get("KAGGLE_KEY")
        or os.environ.get("KAGGLE_TOKEN", "")
    ).strip()
    if kg_key and not crypto.load_key("kaggle"):
        crypto.save_key("kaggle", kg_key, label="Kaggle API Token (env)")

    # Launch Telegram bot worker if configured
    global _telegram_task
    if telegram.is_configured():
        _telegram_task = asyncio.create_task(telegram.run_bot_polling())


@app.on_event("shutdown")
async def _shutdown_tasks() -> None:
    global _telegram_task
    if _telegram_task:
        _telegram_task.cancel()
        try:
            await _telegram_task
        except asyncio.CancelledError:
            pass


HERE = Path(__file__).parent
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


def _asset(path: str) -> str:
    """Cache-busted static URL: ?v=<mtime> forces the browser to refetch on change."""
    try:
        v = int((HERE / "static" / path).stat().st_mtime)
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={v}"


templates.env.globals["asset"] = _asset


# --------------------------------------------------------------------- auth --
def _is_authed(request: Request) -> bool:
    token = request.cookies.get("sid", "")
    return db.check_cookie(token)


def require_auth(request: Request) -> None:
    if not _is_authed(request):
        if core.APP_PASSWORD:
            raise HTTPException(status_code=401, detail="login required")
        # no password: anyone is "authed", but the session is still gated by APP_PASSWORD absence


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    path = request.url.path
    if path.startswith(("/static", "/login", "/logout", "/healthz")):
        return await call_next(request)
    if not _is_authed(request) and core.APP_PASSWORD:
        if path.startswith("/v1/") or path in ("/", "/settings"):
            return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if not core.APP_PASSWORD or _is_authed(request):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "login.html", {})


# ------------------------------------------------- login rate limiting (brute-force defense) --
# In-memory per-IP failure tracking: max 5 failed logins per IP per hour.
# Process-local (fine for single-process uvicorn); resets on restart.
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 3600
_login_failures: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    """Best-effort client IP: honor X-Forwarded-For (set by Caddy) else socket peer."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _login_blocked(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _login_failures.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
    _login_failures[ip] = recent
    return len(recent) >= LOGIN_MAX_FAILURES


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if not core.APP_PASSWORD:
        return RedirectResponse("/", status_code=303)
    ip = _client_ip(request)
    if _login_blocked(ip):
        return templates.TemplateResponse(
            request, "login.html", {"error": "too many failed attempts, try again later"},
            status_code=429,
        )
    import hmac
    if not hmac.compare_digest(password, core.APP_PASSWORD):
        _login_failures.setdefault(ip, []).append(time.time())
        return templates.TemplateResponse(
            request, "login.html", {"error": "wrong password"}, status_code=401
        )
    _login_failures.pop(ip, None)  # success → clear the counter
    tok = db.make_cookie_token()
    resp = RedirectResponse("/", status_code=303)
    kwargs = {"httponly": True, "samesite": "lax"}
    if core.APP_PUBLIC:
        kwargs["secure"] = True
    resp.set_cookie("sid", tok, **kwargs)
    return resp


@app.get("/logout")
async def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("sid")
    return resp


# ----------------------------------------------------------------------- UI --
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"sessions": sessions.list_all()},
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "providers": crypto.list_providers(),
            "mcp_config": core.MCP_CONFIG_PATH.read_text() if core.MCP_CONFIG_PATH.exists() else "",
            "public_mode": core.APP_PUBLIC,
        },
    )


# --------------------------------------------------------------------- keys --
@app.get("/v1/keys")
async def list_keys():
    out = []
    for p in crypto.list_providers():
        meta = crypto.load_key(p) or {}
        out.append({"provider": p, "label": meta.get("label"), "base_url": meta.get("base_url")})
    return out


@app.post("/v1/keys")
async def upsert_key(payload: dict):
    provider = payload["provider"]
    key = payload["key"]
    crypto.save_key(provider, key, payload.get("base_url"), payload.get("label"))
    return {"ok": True}


@app.delete("/v1/keys/{provider}")
async def delete_key(provider: str):
    crypto.delete_key(provider)
    return {"ok": True}


# ----------------------------------------------------------------- mcp.json --
@app.post("/v1/mcp")
async def save_mcp(payload: dict):
    raw = payload.get("raw", "")
    try:
        parsed = json.loads(raw) if raw.strip() else {"mcpServers": {}}
        if "mcpServers" not in parsed:
            parsed = {"mcpServers": parsed}
    except Exception as e:
        raise HTTPException(400, f"invalid JSON: {e}")
    core.MCP_CONFIG_PATH.write_text(json.dumps(parsed, indent=2))
    return {"ok": True}


# -------------------------------------------------------------- sessions API --
@app.get("/v1/sessions")
async def list_sessions():
    return sessions.list_all()


@app.post("/v1/sessions")
async def create_session(payload: dict):
    if not crypto.load_key(payload["provider"]):
        raise HTTPException(400, f"no key stored for provider {payload['provider']!r}")
    row = sessions.create(
        provider=payload["provider"],
        model=payload["model"],
        base_url=payload.get("base_url"),
        mcp_overrides=payload.get("mcp_overrides"),
        title=payload.get("title"),
    )
    return row


@app.get("/v1/sessions/{sid}")
async def get_session(sid: str):
    row = sessions.get(sid)
    if not row:
        raise HTTPException(404, "no such session")
    return row


@app.delete("/v1/sessions/{sid}")
async def delete_session(sid: str):
    sessions.delete(sid)
    return {"ok": True}


@app.post("/v1/sessions/{sid}/title")
async def rename(sid: str, payload: dict):
    db.update_title(sid, payload.get("title", "untitled"))
    return {"ok": True}


# --------------------------------------------------- model catalog (picker) --
# Curated list per provider. OpenRouter is fetched live at request time and
# merged with the curated list so the picker always has the recent catalog
# (and we have a sensible fallback when the network is down).
from .model_catalog import CURATED, openrouter_live_models

@app.get("/v1/models")
async def list_models(provider: str | None = None):
    out = {"providers": {}}
    for prov, items in CURATED.items():
        out["providers"][prov] = items
    # Live enrichment for openrouter (best-effort, short timeout)
    if (provider is None or provider == "openrouter") and "openrouter" in (provider or "openrouter",):
        try:
            live = await openrouter_live_models()
            if live:
                out["providers"]["openrouter"] = live + [
                    m for m in CURATED.get("openrouter", [])
                    if not any(x["id"] == m["id"] for x in live)
                ]
        except Exception:
            pass
    return out


@app.get("/v1/sessions/{sid}/usage")
async def session_usage(sid: str):
    return db.get_usage(sid) or {}


@app.get("/v1/sessions/{sid}/history")
async def session_history(sid: str):
    """Rebuilt chat history (from the on-disk SDK transcript) for page reloads."""
    row = sessions.get(sid)
    if not row:
        raise HTTPException(404, "no such session")
    try:
        return await asyncio.to_thread(sessions.load_history, sid)
    except Exception as e:
        return {"messages": [], "last_usage": None, "error": str(e)}


@app.get("/v1/sessions/{sid}/context")
async def session_context(sid: str):
    row = sessions.get(sid)
    if not row:
        raise HTTPException(404, "no such session")
    try:
        active = await sessions.get_or_start(sid)
        return await sessions.get_context_usage(active)
    except Exception as e:
        return {"error": str(e)}


@app.post("/v1/sessions/{sid}/compact")
async def session_compact(sid: str):
    row = sessions.get(sid)
    if not row:
        raise HTTPException(404, "no such session")
    try:
        active = await sessions.get_or_start(sid)
        return await sessions.compact_now(active)
    except Exception as e:
        return {"error": str(e)}


# --------------------------------------------------- files / workspace view --
@app.get("/v1/sessions/{sid}/files")
async def list_files(sid: str):
    row = sessions.get(sid)
    if not row:
        raise HTTPException(404, "no such session")
    ws = Path(row["workspace"])
    out = []
    if ws.exists():
        for p in ws.rglob("*"):
            if p.is_file():
                rel = p.relative_to(ws).as_posix()
                out.append({"path": rel, "size": p.stat().st_size})
    return out


@app.get("/v1/sessions/{sid}/files/raw")
async def read_file(sid: str, path: str):
    from fastapi.responses import FileResponse
    row = sessions.get(sid)
    if not row:
        raise HTTPException(404, "no such session")
    full = (Path(row["workspace"]) / path).resolve()
    ws_root = Path(row["workspace"]).resolve()
    if not str(full).startswith(str(ws_root)):
        raise HTTPException(403, "path escapes workspace")
    if not full.exists() or not full.is_file():
        raise HTTPException(404, "no such file")
    # Browsers (Chrome in particular) will NOT render text/csv, text/markdown,
    # etc. inline — they force a download even with Content-Disposition: inline.
    # They DO render text/plain inline. So for text-based files we serve them as
    # text/plain so the content shows in the tab; the download still saves the
    # correct bytes + filename (taken from Content-Disposition, not the MIME).
    # Images / PDFs keep their real MIME so they render natively.
    import mimetypes
    guessed = mimetypes.guess_type(full.name)[0] or "application/octet-stream"
    media_type = guessed
    if guessed.startswith("text/") and guessed not in ("text/plain", "text/html"):
        media_type = "text/plain; charset=utf-8"
    return FileResponse(
        full,
        filename=full.name,
        media_type=media_type,
        content_disposition_type="inline",
    )


# ------------------------------------------------- dataset upload (data/) --
# Accepted dataset extensions. Anything else is rejected so the workspace
# data/ dir stays a clean, predictable landing zone for the agent.
_DATASET_EXTS = {".csv", ".tsv", ".parquet", ".xlsx", ".xls", ".json", ".jsonl", ".feather", ".h5", ".hdf5", ".pkl", ".pickle", ".npy", ".npz"}
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


def _safe_dataset_name(name: str) -> str:
    """Flatten to a single path segment, keep the extension."""
    import re
    base = Path(name).name or "dataset"
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)[:150]
    return base or "dataset"


@app.post("/v1/sessions/{sid}/upload")
async def upload_dataset(sid: str, file: UploadFile = File(...)):
    """Upload a dataset into the session workspace's data/ directory.

    The agent is told (system prompt) that uploaded datasets live in
    <workspace>/data/ — it can then ds_preview() them directly.
    """
    row = sessions.get(sid)
    if not row:
        raise HTTPException(404, "no such session")
    ws = Path(row["workspace"])
    data_dir = ws / "data"
    data_dir.mkdir(exist_ok=True)

    ext = Path(file.filename or "").suffix.lower()
    if ext not in _DATASET_EXTS:
        raise HTTPException(400, f"unsupported dataset type {ext!r} — allowed: {sorted(_DATASET_EXTS)}")

    name = _safe_dataset_name(file.filename or "dataset")
    dest = data_dir / name
    # Avoid clobbering an existing file: dataset.csv → dataset-1.csv, ...
    n = 1
    while dest.exists():
        dest = data_dir / f"{Path(name).stem}-{n}{ext}"
        n += 1

    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(8 * 1024 * 1024):
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, "file exceeds 2 GB limit")
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(500, f"upload failed: {e}")

    rel = dest.relative_to(ws).as_posix()
    return {"ok": True, "path": rel, "size": size,
            "hint": f"dataset saved at data/{dest.name} — use ds_preview(path='{rel}')"}


# ------------------------------------------------------ WebSocket bridge ---
@app.websocket("/ws/sessions/{sid}")
async def ws_session(ws: WebSocket, sid: str):
    if core.APP_PASSWORD and not db.check_cookie(ws.cookies.get("sid", "")):
        await ws.close(code=4401)
        return
    await ws.accept()
    try:
        active = await sessions.get_or_start(sid)
    except Exception as e:
        import traceback
        await ws.send_text(json.dumps({"type": "error", "message": str(e), "trace": traceback.format_exc()}))
        await ws.close()
        return

    active.in_use = True
    reader_task = None
    try:
        # Greet
        await ws.send_text(json.dumps({
            "type": "ready",
            "session_id": sid,
            "title": active.db_row.get("title"),
            "model": active.db_row.get("model"),
            "provider": active.db_row.get("provider"),
            "workspace": str(active.workspace),
        }))

        async def reader():
            try:
                async for frame in sessions.stream_events(active):
                    # Forward all frame types; artifact rewriting already happened
                    # in stream_events. Skip only the keepalive 'result' type's
                    # internal subtypes that the UI doesn't need.
                    try:
                        await ws.send_text(json.dumps(frame, default=str))
                    except Exception:
                        break
            except Exception as e:
                import traceback
                try:
                    await ws.send_text(json.dumps({"type": "reader_error", "message": str(e), "trace": traceback.format_exc()}))
                except Exception:
                    pass

        reader_task = asyncio.create_task(reader())

        while True:
            try:
                raw = await ws.receive_text()
            except WebSocketDisconnect:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("type")
            if t == "user":
                await sessions.send_user_message(active, msg.get("text", ""))
            elif t == "interrupt":
                await sessions.interrupt(active)
            elif t == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    finally:
        active.in_use = False
        # stream_events is long-lived (survives across turns); cancel the
        # reader so its internal pump tasks don't leak after the WS closes.
        if reader_task is not None:
            reader_task.cancel()


@app.get("/healthz")
async def healthz():
    return {"ok": True}
