"""Telegram bot integration for ds-agent.

Allows interacting with the data-science agent directly from Telegram.
Runs in background alongside FastAPI when TELEGRAM_BOT_TOKEN is set in .env.

Configuration in .env:
  TELEGRAM_BOT_TOKEN="123456789:ABCdefGHIjklMNOpqrSTUvwxYZ"
  TELEGRAM_ALLOWED_USERS="12345678,87654321"   # Optional whitelist of Telegram user IDs

Features:
- /start, /help - Welcome and usage instructions
- /new - Start a fresh agent session
- /sessions - List recent sessions
- /switch <id> - Switch active session
- /compact - Compact context window
- /stop - Interrupt running turn
- Plain text messages sent directly to the agent; streaming/turn results sent back.
- Photos/plots/files generated as artifacts sent as Telegram media attachments.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from pathlib import Path
from typing import Any
import urllib.parse
import urllib.request

from . import core, db, crypto, sessions, model_catalog

logger = logging.getLogger("ds_agent.telegram")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ALLOWED_USERS = [
    int(uid.strip())
    for uid in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
    if uid.strip().isdigit()
]

# Track current active session_id per Telegram chat_id
_chat_sessions: dict[int, str] = {}
# Lock per chat so turns don't overlap
_chat_locks: dict[int, asyncio.Lock] = {}


def is_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN)


class TelegramAPI:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}"

    def _request(self, method: str, data: dict | None = None, files: dict | None = None) -> dict:
        url = f"{self.base_url}/{method}"
        if files:
            # Multipart form-data for uploading documents/photos
            boundary = "----TelegramFormBoundary" + os.urandom(16).hex()
            body = bytearray()
            if data:
                for k, v in data.items():
                    body.extend(f"--{boundary}\r\n".encode())
                    body.extend(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
                    body.extend(f"{v}\r\n".encode())
            for field_name, (filename, file_bytes, mime_type) in files.items():
                body.extend(f"--{boundary}\r\n".encode())
                body.extend(
                    f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode()
                )
                body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode())
                body.extend(file_bytes)
                body.extend(b"\r\n")
            body.extend(f"--{boundary}--\r\n".encode())
            req = urllib.request.Request(
                url, data=bytes(body), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
            )
        else:
            payload = json.dumps(data or {}).encode("utf-8")
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )

        try:
            with urllib.request.urlopen(req, timeout=35) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.error("Telegram API %s failed: %s", method, e)
            return {"ok": False, "description": str(e)}

    async def call(self, method: str, data: dict | None = None, files: dict | None = None) -> dict:
        return await asyncio.to_thread(self._request, method, data, files)

    async def send_message(self, chat_id: int, text: str, parse_mode: str = "Markdown") -> dict:
        # Split message if exceeds Telegram 4096 char limit
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)] or ["(empty)"]
        res = {}
        for chunk in chunks:
            # Try markdown first, fallback to plain text if malformed Markdown
            res = await self.call("sendMessage", {"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode})
            if not res.get("ok") and parse_mode:
                res = await self.call("sendMessage", {"chat_id": chat_id, "text": chunk})
        return res

    async def send_photo(self, chat_id: int, photo_bytes: bytes, caption: str = "") -> dict:
        return await self.call(
            "sendPhoto",
            {"chat_id": str(chat_id), "caption": caption[:1024]},
            {"photo": ("plot.png", photo_bytes, "image/png")},
        )

    async def send_document(self, chat_id: int, doc_bytes: bytes, filename: str, caption: str = "") -> dict:
        return await self.call(
            "sendDocument",
            {"chat_id": str(chat_id), "caption": caption[:1024]},
            {"document": (filename, doc_bytes, "application/octet-stream")},
        )


def _ensure_session_for_chat(chat_id: int) -> str:
    """Return active session for chat, or create one with default provider."""
    sid = _chat_sessions.get(chat_id)
    if sid and db.get_session(sid):
        return sid

    # Get recent sessions
    recent = db.list_sessions()
    if recent:
        sid = recent[0]["id"]
        _chat_sessions[chat_id] = sid
        return sid

    # Find default provider
    provs = crypto.list_providers()
    provider = "openrouter" if "openrouter" in provs else (provs[0] if provs else "openrouter")
    model = "anthropic/claude-sonnet-4-5" if provider == "openrouter" else "claude-3-7-sonnet"
    new_sess = sessions.create(
        provider=provider,
        model=model,
        title=f"Telegram Chat {chat_id}",
    )
    _chat_sessions[chat_id] = new_sess["id"]
    return new_sess["id"]


async def _handle_command(api: TelegramAPI, chat_id: int, text: str) -> None:
    parts = text.strip().split()
    cmd = parts[0].lower()

    if cmd in ("/start", "/help"):
        msg = (
            "🤖 *Welcome to ds-agent Telegram Bridge!*\n\n"
            "You can interact directly with your Data Science agent here.\n\n"
            "*Commands:*\n"
            "• `/new [model_id]` - Create a new session (optionally specify model)\n"
            "• `/models` - List popular model IDs to use with `/new`\n"
            "• `/sessions` - List existing sessions\n"
            "• `/switch <id>` - Switch to an existing session\n"
            "• `/compact` - Compact context window\n"
            "• `/stop` - Interrupt the current running turn\n"
            "• `/status` - Show current active session info\n\n"
            "Send any prompt or question to start working with the agent!"
        )
        await api.send_message(chat_id, msg)
        return

    if cmd == "/models":
        lines = ["*Available Models:*"]
        for prov, mlist in model_catalog.CURATED.items():
            if prov == "custom":
                continue
            lines.append(f"\n*{prov.upper()}:*")
            for m in mlist:
                if m["id"]:
                    lines.append(f"• `{m['id']}` - _{m['label']}_")
        lines.append("\nUse `/new <model_id>` to start a session with that model.")
        await api.send_message(chat_id, "\n".join(lines))
        return

    if cmd == "/new":
        specified_model = parts[1].strip() if len(parts) > 1 else ""
        provs = crypto.list_providers()

        provider = "openrouter" if "openrouter" in provs else (provs[0] if provs else "openrouter")
        if specified_model:
            model = specified_model
            # Determine provider if format is provider/model or known provider
            if "/" in model:
                provider = "openrouter"
            elif model.startswith("claude-"):
                provider = "anthropic" if "anthropic" in provs else "openrouter"
            elif model.lower().startswith("minimax"):
                provider = "minimax" if "minimax" in provs else "openrouter"
        else:
            model = "anthropic/claude-sonnet-4-5" if provider == "openrouter" else "claude-3-7-sonnet"

        sess = sessions.create(
            provider=provider,
            model=model,
            title=f"Telegram Session {len(db.list_sessions()) + 1}",
        )
        _chat_sessions[chat_id] = sess["id"]
        await api.send_message(chat_id, f"✅ Created new session `{sess['id']}` ({provider} / `{model}`)")
        return

    if cmd == "/sessions":
        sess_list = db.list_sessions()[:10]
        if not sess_list:
            await api.send_message(chat_id, "No sessions found. Use `/new` to create one.")
            return
        cur_id = _chat_sessions.get(chat_id)
        lines = ["*Recent Sessions:*"]
        for s in sess_list:
            active_marker = " 👈 *(current)*" if s["id"] == cur_id else ""
            lines.append(f"• `{s['id'][:8]}` - *{s['title']}* ({s['model']}){active_marker}")
        lines.append("\nSwitch using `/switch <id>`")
        await api.send_message(chat_id, "\n".join(lines))
        return

    if cmd == "/switch":
        if len(parts) < 2:
            await api.send_message(chat_id, "Usage: `/switch <session_id>`")
            return
        target = parts[1]
        all_s = db.list_sessions()
        matched = [s for s in all_s if s["id"].startswith(target)]
        if not matched:
            await api.send_message(chat_id, f"❌ No session matching `{target}`")
            return
        _chat_sessions[chat_id] = matched[0]["id"]
        await api.send_message(chat_id, f"Switched to `{matched[0]['id']}` (*{matched[0]['title']}*)")
        return

    if cmd == "/compact":
        sid = _ensure_session_for_chat(chat_id)
        try:
            active = await sessions.get_or_start(sid)
            res = await sessions.compact_now(active)
            await api.send_message(chat_id, f"🧹 Context compacted. Status: `{res.get('status')}`")
        except Exception as e:
            await api.send_message(chat_id, f"❌ Compact error: {e}")
        return

    if cmd == "/stop":
        sid = _ensure_session_for_chat(chat_id)
        active = sessions.get_active(sid)
        if active:
            await sessions.interrupt(active)
            await api.send_message(chat_id, "🛑 Interrupted running agent.")
        else:
            await api.send_message(chat_id, "Agent is currently idle.")
        return

    if cmd == "/status":
        sid = _ensure_session_for_chat(chat_id)
        row = db.get_session(sid)
        if row:
            msg = (
                f"📊 *Current Session:* `{row['id']}`\n"
                f"• *Title:* {row['title']}\n"
                f"• *Model:* `{row['model']}`\n"
                f"• *Provider:* `{row['provider']}`\n"
                f"• *Workspace:* `{row['workspace']}`"
            )
            await api.send_message(chat_id, msg)
        return


async def _run_agent_turn_for_telegram(api: TelegramAPI, chat_id: int, user_text: str) -> None:
    sid = _ensure_session_for_chat(chat_id)
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()

    async with _chat_locks[chat_id]:
        try:
            active = await sessions.get_or_start(sid)
        except Exception as e:
            await api.send_message(chat_id, f"❌ Failed to start agent session: {e}")
            return

        # Send initial typing indicator
        await api.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        await sessions.send_user_message(active, user_text)

        accumulated_text = []
        last_typing_time = asyncio.get_event_loop().time()

        try:
            async for frame in sessions.stream_events(active):
                ftype = frame.get("type")

                # Heartbeat / typing action
                now = asyncio.get_event_loop().time()
                if now - last_typing_time > 4.0:
                    await api.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
                    last_typing_time = now

                if ftype == "assistant":
                    content = frame.get("content") or []
                    for b in content:
                        if b.get("type") == "text" and b.get("text"):
                            accumulated_text.append(b.get("text"))

                elif ftype == "error":
                    await api.send_message(chat_id, f"⚠️ *Agent Error:* {frame.get('message')}")
                    return

                elif ftype == "result":
                    # Send final accumulated response
                    final_text = "".join(accumulated_text).strip()
                    if final_text:
                        await api.send_message(chat_id, final_text)

                    # Check for created plots / artifacts in session workspace
                    ws_dir = Path(active.db_row["workspace"])
                    if ws_dir.exists():
                        # Find recently modified image files
                        for img in ws_dir.glob("*.png"):
                            try:
                                if img.stat().st_mtime >= (now - 120):  # created/modified in last 2m
                                    await api.send_photo(chat_id, img.read_bytes(), caption=img.name)
                            except Exception:
                                pass

                    cost = frame.get("total_cost_usd")
                    cost_str = f" (${cost:.4f})" if cost is not None else ""
                    await api.send_message(chat_id, f"✅ _Turn finished_{cost_str}")
                    return
        except Exception as e:
            logger.error("Error during Telegram agent turn: %s", e)
            await api.send_message(chat_id, f"❌ Turn error: {e}")


async def run_bot_polling() -> None:
    """Long-polling background worker for Telegram bot."""
    if not is_configured():
        logger.info("Telegram bot not configured (TELEGRAM_BOT_TOKEN empty)")
        return

    logger.info("Starting Telegram bot long-polling...")
    api = TelegramAPI(TELEGRAM_BOT_TOKEN)
    offset = 0

    while True:
        try:
            updates_res = await api.call("getUpdates", {"offset": offset, "timeout": 30, "limit": 20})
            if not updates_res.get("ok"):
                await asyncio.sleep(5)
                continue

            for u in updates_res.get("result", []):
                offset = max(offset, u["update_id"] + 1)
                msg = u.get("message")
                if not msg:
                    continue

                chat = msg.get("chat", {})
                chat_id = chat.get("id")
                from_user = msg.get("from", {})
                user_id = from_user.get("id")
                text = msg.get("text", "")

                if not chat_id or not text:
                    continue

                # Access control check
                if TELEGRAM_ALLOWED_USERS and user_id not in TELEGRAM_ALLOWED_USERS:
                    await api.send_message(
                        chat_id,
                        f"⛔ Access denied. Your Telegram user ID is `{user_id}`. Add it to `TELEGRAM_ALLOWED_USERS`."
                    )
                    continue

                if text.startswith("/"):
                    await _handle_command(api, chat_id, text)
                else:
                    asyncio.create_task(_run_agent_turn_for_telegram(api, chat_id, text))

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Telegram polling loop error: %s", e)
            await asyncio.sleep(5)
