"""Session lifecycle: in-memory active sessions + persistence to SQLite."""
from __future__ import annotations
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

from . import core, db, providers as prov_mod
from .artifact_parser import rewrite_tool_result_content
from . import agent_prompt
from .trim import trim_tool_result_blocks

# In-memory registry of active SDK clients, keyed by session_id.
# Each WebSocket connection borrows the client, but only one WS is allowed at a time.
_active: dict[str, "ActiveSession"] = {}
_active_locks: dict[str, asyncio.Lock] = {}


def _new_set_event() -> asyncio.Event:
    ev = asyncio.Event()
    ev.set()
    return ev


@dataclass
class ActiveSession:
    session_id: str
    db_row: dict
    workspace: Path
    client: ClaudeSDKClient
    in_use: bool = False
    pending_steer: asyncio.Queue = field(default_factory=asyncio.Queue)
    # True while a turn is in flight (set by the steer pump, cleared on the
    # result frame). The stream watchdog is only armed while this is set, so
    # idle time between turns never triggers a false timeout.
    turn_active: bool = False
    # Set whenever no turn is in flight; cleared the moment the steer pump
    # sends a query(). The pump waits on this before sending the next queued
    # message, so a message that arrives while a turn is still generating is
    # held until that turn's result frame lands instead of being injected
    # into the CLI's stdin mid-turn (which caused replies to answer the
    # wrong queued question — see _steer_pump).
    turn_done: asyncio.Event = field(default_factory=_new_set_event)
    # Single shared reader over active.client.receive_messages(), started
    # lazily and fanned out to every stream_events() subscriber (web UI WS,
    # Telegram, ...). Do NOT let callers spin up their own reader per call —
    # two tasks iterating the same SDK message stream race for each message,
    # so whichever consumer doesn't win a given message just never sees it
    # (and its watchdog then "recovers" a turn that was actually fine,
    # interrupting the other consumer's in-flight turn). See docs/debug_notes.md
    # "simultaneous web UI + Telegram" incident.
    _engine_task: asyncio.Task | None = field(default=None, repr=False)
    _subscribers: list[asyncio.Queue] = field(default_factory=list, repr=False)


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def create(*, provider: str, model: str, base_url: str | None = None,
            mcp_overrides: dict | None = None, title: str | None = None) -> dict:
    sid = _new_id()
    workspace = core.DATA_DIR / "workspaces" / sid
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "data").mkdir(exist_ok=True)  # dataset upload landing zone
    row = {
        "id": sid,
        "title": title or f"New session {sid[:6]}",
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "mcp_overrides": json.dumps(mcp_overrides or {}),
        "workspace": str(workspace),
    }
    db.create_session(row)
    return row


def list_all() -> list[dict]:
    return db.list_sessions()


def get(sid: str) -> dict | None:
    return db.get_session(sid)


def get_active(sid: str) -> ActiveSession | None:
    """The in-memory ActiveSession if one is currently spawned, else None."""
    return _active.get(sid)


def delete(sid: str) -> None:
    if sid in _active:
        try:
            asyncio.create_task(_active[sid].client.disconnect())
        except Exception:
            pass
        _active.pop(sid, None)
    db.delete_session(sid)
    # best-effort wipe of workspace + transcript
    row = db.get_session(sid)
    if row:
        import shutil
        for p in (Path(row["workspace"]), core.DATA_DIR / "sessions" / sid):
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)


async def get_or_start(sid: str) -> ActiveSession:
    """Return the active session, spawning the SDK client if needed.

    If a cached session's CLI subprocess has died (crash, or killed to pick
    up a changed MCP config), respawn the client so the caller gets a usable
    connection instead of writing into a dead pipe.
    """
    if sid in _active:
        active = _active[sid]
        if client_alive(active):
            return active
        # Dead CLI — respawn under the lock so concurrent callers don't race.
        lock = _active_locks.setdefault(sid, asyncio.Lock())
        async with lock:
            if sid in _active and client_alive(_active[sid]):
                return _active[sid]
            await respawn(_active[sid])
            return _active[sid]
    lock = _active_locks.setdefault(sid, asyncio.Lock())
    async with lock:
        if sid in _active:
            return _active[sid]
        row = db.get_session(sid)
        if not row:
            raise KeyError(sid)
        active = await _spawn(row)
        _active[sid] = active
        return active


async def _spawn_client(row: dict) -> ClaudeSDKClient:
    """Create and connect a ClaudeSDKClient for the given session row."""
    from . import crypto
    stored = crypto.load_key(row["provider"])
    if not stored:
        raise RuntimeError(f"No key stored for provider {row['provider']!r}")
    env = prov_mod.env_for(row["provider"], stored["key"], row.get("base_url"), row["model"])

    workspace = Path(row["workspace"])
    workspace.mkdir(parents=True, exist_ok=True)

    # Compose per-session .mcp.json + .claude/settings.local.json
    await asyncio.to_thread(_render_session_dir, row, env, workspace)

    opts = ClaudeAgentOptions(
        cwd=str(workspace),
        setting_sources=["project"],  # ignore host's ~/.claude/
        env=env,
        max_budget_usd=core.MAX_BUDGET_USD,
        # Single-user personal tool: bypass permission prompts. For a public-host
        # deployment, swap this for `can_use_tool` to gate sensitive actions.
        permission_mode="bypassPermissions",
        # The 0.2.x SDK uses extra_args to forward CLI flags. --append-system-prompt
        # adds our data-science guidance on top of the default prompt.
        extra_args={"append-system-prompt": agent_prompt.build_append_system_prompt()},
        # Resume the prior conversation: the SDK needs its own session UUID
        # (from the transcript), not our sid.
        resume=_sdk_session_id(workspace),
    )
    client = ClaudeSDKClient(opts)
    await client.connect()
    return client


async def _spawn(row: dict) -> ActiveSession:
    """Render the session dir and spawn a ClaudeSDKClient subprocess."""
    client = await _spawn_client(row)
    return ActiveSession(session_id=row["id"], db_row=row, workspace=Path(row["workspace"]), client=client)


def _has_transcript(sid: str) -> bool:
    p = core.DATA_DIR / "sessions" / sid / "transcript.jsonl"
    return p.exists() and p.stat().st_size > 0


def _latest_transcript(workspace: Path) -> Path | None:
    """Find the most recent SDK transcript for this session's workspace.

    The Claude CLI stores transcripts at
    ~/.claude/projects/<cwd with every non-alphanumeric char replaced by
    '-'>/<uuid>.jsonl — one file per CLI process. The newest mtime is the
    current conversation.
    """
    import re as _re
    slug = _re.sub(r"[^a-zA-Z0-9]", "-", str(workspace))
    proj = Path.home() / ".claude" / "projects" / slug
    if not proj.is_dir():
        return None
    files = [p for p in proj.glob("*.jsonl") if p.stat().st_size > 0]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _sdk_session_id(workspace: Path) -> str | None:
    """The Claude Code session UUID for resume (from the latest transcript).

    Our sid is NOT the SDK session id — the CLI generates its own UUID per
    process, which is the transcript filename (and every entry's
    `sessionId` field).
    """
    tp = _latest_transcript(workspace)
    return tp.stem if tp else None


def load_history(sid: str, inline_artifacts: bool = True) -> dict:
    """Rebuild the chat history for the UI from the on-disk SDK transcript.

    Returns {messages: [...], last_usage: {...} | None} where each message has
    the same shape the WebSocket frames produce client-side:
      {role: "user"|"assistant"|"tool"|"tool-result", content: <raw>}
    Tool-result text is passed through the artifact rewriter so plots/files
    embedded via __ARTIFACT__ markers render again after a page refresh.

    `inline_artifacts=False` skips that rewrite (leaves the raw
    `__ARTIFACT__:kind:path` markers in the text) — used by search/export,
    where base64-inlining every plot into memory is wasted work.
    """
    row = db.get_session(sid)
    if not row:
        raise KeyError(sid)
    workspace = Path(row["workspace"])
    tp = _latest_transcript(workspace)
    messages: list[dict] = []

    def _maybe_rewrite(text: str) -> str:
        return rewrite_tool_result_content(text, workspace=workspace) if inline_artifacts else text

    if tp:
        for line in tp.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            t = e.get("type")
            if t == "user":
                c = (e.get("message") or {}).get("content")
                if isinstance(c, str) and c.strip():
                    messages.append({"role": "user", "content": c})
                elif isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            inner = b.get("content")
                            if isinstance(inner, str):
                                inner = _maybe_rewrite(inner)
                            messages.append({"role": "tool-result", "content": inner})
            elif t == "assistant":
                c = (e.get("message") or {}).get("content")
                if isinstance(c, list):
                    for b in c:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "text" and b.get("text", "").strip():
                            messages.append({"role": "assistant", "content": _maybe_rewrite(b["text"])})
                        elif bt == "thinking" and b.get("thinking", "").strip():
                            messages.append({"role": "thinking", "content": b["thinking"]})
                        elif bt == "tool_use":
                            messages.append({"role": "tool", "content": b})
    usage_row = db.get_usage(sid) or {}
    return {"messages": messages, "last_usage": usage_row.get("usage")}


def _render_session_dir(row: dict, env: dict, workspace: Path) -> None:
    """Write .mcp.json and .claude/settings.local.json into the workspace root.

    Resolves ${VAULT:provider_key} placeholders in the MCP env / headers against
    the encrypted key store, so spawned MCP subprocesses see the actual secret
    rather than the literal placeholder string.
    """
    from . import crypto
    import re
    overrides = json.loads(row.get("mcp_overrides") or "{}")
    if core.MCP_CONFIG_PATH.exists():
        global_cfg = json.loads(core.MCP_CONFIG_PATH.read_text())
    else:
        global_cfg = {"mcpServers": {}}

    # Path placeholders so mcp.json stays host-agnostic:
    #   ${ROOT}     → project checkout root (where mcp.json / src/ live)
    #   ${DATA_DIR} → ~/.coding-agent (or $CODING_AGENT_HOME)
    #   ${VAULT:k}  → BYOK key stored under provider name k
    _PLACEHOLDERS = {"ROOT": str(core.PROJECT_ROOT), "DATA_DIR": str(core.DATA_DIR)}

    def _resolve(node):
        if isinstance(node, dict):
            return {k: _resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_resolve(x) for x in node]
        if isinstance(node, str):
            def _sub(m):
                inner = m.group(1)
                if inner in _PLACEHOLDERS:
                    return _PLACEHOLDERS[inner]
                if inner.startswith("VAULT:"):
                    meta = crypto.load_key(inner.split(":", 1)[1])
                    if meta and meta.get("key"):
                        return meta["key"]
                return m.group(0)  # unknown / unresolved → leave as-is
            return re.sub(r"\$\{([A-Z_]+(?::\w+)?)\}", _sub, node)
        return node

    mcp_servers = {**global_cfg.get("mcpServers", {}), **overrides.get("mcpServers", {})}
    mcp_servers = _resolve(mcp_servers)

    workspace.joinpath(".mcp.json").write_text(
        json.dumps({"mcpServers": mcp_servers}, indent=2)
    )
    workspace.joinpath(".claude").mkdir(exist_ok=True)
    workspace.joinpath(".claude", "settings.local.json").write_text(
        json.dumps({"env": env}, indent=2)
    )


async def send_user_message(active: ActiveSession, text: str) -> None:
    """Queue a user message; the WS reader loop will call query() on it.

    Proactively auto-compacts first if context is >= 95% full.
    """
    db.touch_session(active.session_id)

    # Proactive auto-compact: if context is >= 95% full, trigger compact before query
    try:
        usage = await active.client.get_context_usage()
        pct = float(usage.get("percentage", 0) or 0)
        if pct >= 95.0:
            import logging
            logging.getLogger("ds_agent.sessions").info("Auto-compacting session %s (context at %.1f%%)", active.session_id, pct)
            await active.client.query("/compact")
    except Exception:
        pass

    await active.pending_steer.put({"type": "user", "text": text})

async def interrupt(active: ActiveSession) -> None:
    """Cancel the in-flight turn."""
    try:
        await active.client.interrupt()
    except Exception:
        pass


def client_alive(active: ActiveSession) -> bool:
    """True if the SDK client's CLI subprocess is still running.

    After an interrupt that kills the CLI (or a crash), the in-memory client
    is a zombie: query()/interrupt() raise or hang. The transport keeps the
    anyio Process at transport._process.
    """
    try:
        transport = getattr(active.client, "_transport", None)
        proc = getattr(transport, "_process", None) if transport else None
        if proc is None:
            return False
        return proc.returncode is None
    except Exception:
        return False


async def respawn(active: ActiveSession) -> None:
    """Replace a dead SDK client with a fresh one (resuming the transcript).

    Called when the CLI subprocess has exited (interrupt that killed it, or a
    crash) so the next user message starts a clean process instead of writing
    into a dead pipe.
    """
    try:
        await active.client.disconnect()
    except Exception:
        pass
    try:
        active.client = await _spawn_client(active.db_row)
    except Exception:
        # If respawn fails, drop the session so get_or_start() retries cleanly.
        _active.pop(active.session_id, None)
        raise


async def get_context_usage(active: ActiveSession) -> dict:
    """Return the SDK's context-usage breakdown (for the UI's 'X% of context used').

    The Claude Agent SDK has auto-compact built in: when the context gets
    close to the model's window, the SDK automatically summarizes older turns.
    See isAutoCompactEnabled / autoCompactThreshold in the response.
    """
    try:
        usage = await active.client.get_context_usage()
    except Exception as e:
        return {"error": str(e)}

    # The underlying Claude Code CLI only knows real context-window sizes for
    # Claude models; routed through OpenRouter to a non-Anthropic model (GPT-5,
    # Gemini, ...) it silently reports Claude's own 200K window instead of the
    # real one. Correct maxTokens/percentage using OpenRouter's live per-model
    # context_length when we have it (see model_catalog.get_model_context_window).
    if active.db_row.get("provider") == "openrouter":
        from . import model_catalog
        real_ctx = model_catalog.get_model_context_window(active.db_row.get("model", ""))
        if real_ctx and usage.get("maxTokens") != real_ctx:
            usage = dict(usage)
            total = usage.get("totalTokens", 0) or 0
            usage["maxTokens"] = real_ctx
            usage["rawMaxTokens"] = real_ctx
            usage["contextWindow"] = real_ctx
            usage["percentage"] = min(100.0, (total / real_ctx) * 100)
    return usage


async def compact_now(active: ActiveSession) -> dict:
    """Ask the SDK to compact the session right now (regardless of threshold).

    The SDK triggers the same auto-compact logic, just without waiting for
    the threshold. Returns a brief status dict.
    """
    try:
        # The SDK doesn't expose a programmatic "compact" method directly;
        # we send a slash command via query() which is the CLI's way.
        await active.client.query("/compact")
        return {"status": "compacting"}
    except Exception as e:
        return {"error": str(e)}


# Sentinel broadcast to every subscriber when the shared engine stops
# (client disconnected cleanly, or unrecoverably). Distinct from a real
# payload so stream_events() knows to end its generator rather than yield it.
_ENGINE_STOPPED = object()


def _broadcast(active: ActiveSession, item: Any) -> None:
    for q in list(active._subscribers):
        q.put_nowait(item)


def _ensure_engine_started(active: ActiveSession) -> None:
    """Start the single shared reader/watchdog task for this session, if not
    already running. Idempotent — safe to call from every stream_events()
    caller (web UI WS connect, each Telegram message, ...)."""
    if active._engine_task is None or active._engine_task.done():
        active._engine_task = asyncio.create_task(_run_turn_engine(active))


async def _run_turn_engine(active: ActiveSession) -> None:
    """The one reader over active.client.receive_messages() for this session.

    Every stream_events() caller subscribes to this instead of reading the
    SDK stream itself — two readers on the same stream race for each
    message, so a consumer that loses the race just never sees the reply it
    was waiting on (and its watchdog then "recovers" a turn that was actually
    fine, interrupting the other consumer's real one). See docs/debug_notes.md.

    A watchdog detects hung turns (model call or MCP tool that never
    responds) and attempts recovery via interrupt → respawn.
    """
    sender = asyncio.create_task(_steer_pump(active))
    q: asyncio.Queue = asyncio.Queue()

    async def _pump():
        """Read SDK messages into a queue so the main loop can apply timeouts."""
        try:
            async for msg in active.client.receive_messages():
                await q.put(msg)
        except Exception as e:
            await q.put(e)
        finally:
            await q.put(None)  # sentinel: stream ended

    pump_task = asyncio.create_task(_pump())
    try:
        while True:
            # Wait for the next message with an inactivity timeout.
            try:
                item = await asyncio.wait_for(q.get(), timeout=core.TURN_INACTIVITY_TIMEOUT)
            except asyncio.TimeoutError:
                if not active.turn_active:
                    # Idle between turns — not stuck, just waiting for user input.
                    continue
                # A turn is in flight but nothing arrived for TURN_INACTIVITY_TIMEOUT.
                # The model call or an MCP tool is hung. Try to interrupt.
                _broadcast(active, {"type": "system", "subtype": "watchdog",
                       "message": f"no response for {int(core.TURN_INACTIVITY_TIMEOUT)}s — interrupting stuck turn"})
                await interrupt(active)
                try:
                    item = await asyncio.wait_for(q.get(), timeout=core.TURN_RECOVERY_TIMEOUT)
                except asyncio.TimeoutError:
                    # Interrupt didn't produce a result. Check if the CLI died.
                    if not client_alive(active):
                        _broadcast(active, {"type": "system", "subtype": "watchdog",
                               "message": "agent process died — restarting session"})
                        try:
                            await respawn(active)
                        except Exception as e:
                            _broadcast(active, {"type": "error", "message": f"respawn failed: {e}"})
                            return
                        # Restart the pump with the fresh client.
                        pump_task.cancel()
                        pump_task = asyncio.create_task(_pump())
                        active.turn_active = False
                        active.turn_done.set()
                        continue
                    _broadcast(active, {"type": "error",
                           "message": "turn timed out and could not be recovered — try sending again"})
                    active.turn_active = False
                    active.turn_done.set()
                    continue
                except StopAsyncIteration:
                    return

            if item is None:
                return  # stream ended (client disconnected cleanly)
            if isinstance(item, Exception):
                _broadcast(active, {"type": "error", "message": str(item)})
                continue

            payload = _serialize(item)
            if payload.get("type") == "user":
                # tool result coming back — rewrite content to surface plots / files
                payload = _rewrite_user(payload, workspace=active.workspace)
            if payload.get("type") == "assistant":
                payload = _rewrite_assistant(payload, workspace=active.workspace)
            if payload.get("type") == "result":
                _record_result_usage(active.session_id, item)
                active.turn_active = False
                active.turn_done.set()
            _broadcast(active, payload)
    finally:
        sender.cancel()
        pump_task.cancel()
        _broadcast(active, _ENGINE_STOPPED)


def subscribe(active: ActiveSession) -> asyncio.Queue:
    """Register a fan-out subscriber and ensure the shared engine is running.

    This is synchronous and takes effect immediately — unlike stream_events()
    (an async generator, lazy: nothing in its body runs until first
    iterated). If you're about to send a message and then watch for its
    reply, call subscribe() *before* send_user_message(): otherwise, when the
    engine is already running (e.g. a web UI tab has the session open), the
    steer pump can dispatch and finish your turn before your subscription
    exists, and you silently miss your own reply. See docs/debug_notes.md
    "simultaneous web UI + Telegram" incident.
    """
    _ensure_engine_started(active)
    q: asyncio.Queue = asyncio.Queue()
    active._subscribers.append(q)
    return q


async def stream_from(active: ActiveSession, q: asyncio.Queue) -> Any:
    """Consume a subscription created by subscribe()."""
    try:
        while True:
            item = await q.get()
            if item is _ENGINE_STOPPED:
                return
            yield item
    finally:
        try:
            active._subscribers.remove(q)
        except ValueError:
            pass


async def stream_events(active: ActiveSession) -> Any:
    """Subscribe to this session's shared message stream and consume it.

    Each yielded item is a dict ready to JSON-serialize for the WebSocket.
    Multiple callers (a web UI WebSocket connection, a Telegram turn) can
    subscribe concurrently and safely — they all see the same frames, fanned
    out from the single shared reader (see _run_turn_engine). Do not read
    active.client.receive_messages() directly from anywhere else.

    Convenience wrapper for callers that subscribe and immediately start
    listening with nothing sent in between (e.g. a fresh WS connection before
    its own receive loop starts). If you need to send a message and then
    listen for the reply, use subscribe() + stream_from() instead so you
    register before you send — see subscribe()'s docstring.
    """
    q = subscribe(active)
    async for item in stream_from(active, q):
        yield item


def _record_result_usage(sid: str, msg: Any) -> None:
    """Pull cache + cost stats out of the SDK's ResultMessage and store in DB.

    Calculates accurate cost based on real per-model token pricing from
    model_catalog, with fallback to SDK's costUSD.
    """
    from . import model_catalog
    model_usage = getattr(msg, "model_usage", None) or {}
    total_in = total_out = total_cache_read = total_cache_create = 0
    total_cost = 0.0

    for m_name, u in model_usage.items():
        try:
            in_t = int(u.get("inputTokens") or 0)
            out_t = int(u.get("outputTokens") or 0)
            cache_read_t = int(u.get("cacheReadInputTokens") or 0)
            cache_create_t = int(u.get("cacheCreationInputTokens") or 0)
            total_in += in_t
            total_out += out_t
            total_cache_read += cache_read_t
            total_cache_create += cache_create_t

            # Check if we have exact per-token pricing from OpenRouter/provider catalog
            pricing = model_catalog.get_model_pricing(m_name)
            if pricing:
                prompt_rate = float(pricing.get("prompt") or 0)
                completion_rate = float(pricing.get("completion") or 0)
                cache_read_rate = float(pricing.get("input_cache_read") or (prompt_rate * 0.1))
                cache_write_rate = float(pricing.get("input_cache_write") or (prompt_rate * 1.25))

                calc_cost = (
                    (in_t * prompt_rate) +
                    (out_t * completion_rate) +
                    (cache_read_t * cache_read_rate) +
                    (cache_create_t * cache_write_rate)
                )
                total_cost += calc_cost
            else:
                total_cost += float(u.get("costUSD") or 0)
        except Exception:
            pass

    # If no catalog pricing was matched, fallback to SDK's reported total_cost_usd
    if total_cost <= 0.0 and getattr(msg, "total_cost_usd", None) is not None:
        total_cost = float(msg.total_cost_usd)

    cache_hit_pct = (total_cache_read * 100 // (total_in + total_cache_read + 1)) if (total_in or total_cache_read) else 0
    db.record_usage(sid, {
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_read_tokens": total_cache_read,
        "cache_creation_tokens": total_cache_create,
        "cache_hit_pct": cache_hit_pct,
        "cost_usd": round(total_cost, 6),
        "turn_count": getattr(msg, "num_turns", 0),
        "duration_ms": getattr(msg, "duration_ms", 0),
        "model_usage": {k: dict(v) for k, v in model_usage.items()},
    })


async def _steer_pump(active: ActiveSession) -> None:
    """Drain the steer queue: when a 'user' message arrives, call client.query().

    Waits for any in-flight turn to finish (turn_done) before sending the
    next queued message. Without this, a message that arrives while the CLI
    is still generating a reply to an earlier one gets query()'d straight
    into the same in-flight turn — the model picks it up mid-generation but
    the final reply can still address the older question, silently dropping
    the answer to the newer one. Queuing strictly after turn completion
    makes each message its own turn instead of racing an active one.
    """
    while True:
        item = await active.pending_steer.get()
        if item.get("type") == "user":
            await active.turn_done.wait()
            active.turn_done.clear()
            active.turn_active = True
            try:
                await active.client.query(item["text"])
            except Exception:
                active.turn_active = False
                active.turn_done.set()


def _serialize(msg: Any) -> dict:
    """Best-effort JSON serialization of any SDK message.

    Quirks handled:
    - ResultMessage has no 'type' field; we inject it from the class name.
    - ContentBlock subclasses (TextBlock, ToolUseBlock, ThinkingBlock,
      ToolResultBlock) are dataclasses, not Pydantic models — so they don't
      have model_dump. We use dataclasses.asdict to convert them.
    - Pydantic models embedded inside dataclass fields (e.g. typed dicts) get
      model_dump too, recursively.
    """
    import dataclasses
    cls_name = type(msg).__name__
    type_map = {
        "AssistantMessage": "assistant",
        "UserMessage": "user",
        "SystemMessage": "system",
        "ResultMessage": "result",
        "StreamEvent": "stream_event",
        "ToolResultBlock": "tool_result",
        "ToolUseBlock": "tool_use",
        "TextBlock": "text",
        "ThinkingBlock": "thinking",
    }

    def _conv(v: Any) -> Any:
        if dataclasses.is_dataclass(v) and not isinstance(v, type):
            d = {k: _conv(x) for k, x in dataclasses.asdict(v).items()}
            # Inject a 'type' discriminator from the class name so the consumer
            # can switch on it (TextBlock vs ToolUseBlock vs ThinkingBlock, etc.)
            if "type" not in d:
                d["type"] = type_map.get(type(v).__name__, type(v).__name__.lower())
            return d
        if isinstance(v, list):
            return [_conv(x) for x in v]
        if isinstance(v, dict):
            return {k: _conv(x) for k, x in v.items()}
        if hasattr(v, "model_dump"):
            try:
                return v.model_dump(mode="json", exclude_none=True)
            except Exception:
                return repr(v)
        return v

    out: dict = {}
    if hasattr(msg, "model_dump"):
        try:
            out = msg.model_dump(mode="json", exclude_none=True)
        except Exception:
            pass
    if not out and hasattr(msg, "__dict__"):
        out = {k: v for k, v in msg.__dict__.items() if not k.startswith("_")}
    if not out:
        return {"type": type_map.get(cls_name, cls_name.lower()), "repr": repr(msg)}

    out = _conv(out)
    if "type" not in out:
        out["type"] = type_map.get(cls_name, cls_name.lower())
    return out


def _rewrite_assistant(payload: dict, workspace: Path | None = None) -> dict:
    """Rewrite __ARTIFACT__ markers in assistant text blocks.

    The agent sometimes emits artifact markers in its own text (not just tool
    stdout). We rewrite them the same way as tool results so the UI can render
    them as links/images.
    """
    content = payload.get("content")
    if not isinstance(content, list):
        return payload
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            block["text"] = rewrite_tool_result_content(block.get("text", ""), workspace=workspace)
    return payload


def _rewrite_user(payload: dict, workspace: Path | None = None) -> dict:
    """If a tool_result block contains __ARTIFACT__ markers, replace its content
    with structured image/file blocks. See artifact_parser.py for the marker format.

    Also trims oversized text blocks (see trim.py) so a 5MB file read doesn't
    silently balloon every subsequent turn's cost. Trims happen on the text
    before the artifact marker is processed — the marker extraction looks for
    lines in the head/tail so it still works on trimmed output.

    A UserMessage frame from the SDK has its content blocks at the top level:
    { "type": "user", "content": [ { "type": "tool_result", "content": "...text..." } ] }
    where 'content' on a tool_result can be a plain string OR a list of blocks
    with {type: "text", text: "..."} entries.
    """
    content = payload.get("content")
    if not isinstance(content, list):
        return payload
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_result":
            continue
        tool_name = block.get("tool_use_id") or "tool"
        inner = block.get("content")
        if isinstance(inner, str):
            inner = rewrite_tool_result_content(inner, workspace=workspace)
            if workspace is not None:
                inner, _ = trim_tool_result_blocks(inner, workspace=workspace, tool_name=tool_name)
            block["content"] = inner
        elif isinstance(inner, list):
            for sub in inner:
                if isinstance(sub, dict) and sub.get("type") == "text":
                    sub["text"] = rewrite_tool_result_content(sub.get("text", ""), workspace=workspace)
            if workspace is not None:
                inner, _ = trim_tool_result_blocks(inner, workspace=workspace, tool_name=tool_name)
    return payload
