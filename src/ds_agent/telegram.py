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
import html
import io
import json
import logging
import os
import re
import time
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
# Chat that receives web-login one-time codes (see otp_chat_id() below).
TELEGRAM_OTP_CHAT_ID = os.environ.get("TELEGRAM_OTP_CHAT_ID", "").strip()

# Track current active session_id per Telegram chat_id
_chat_sessions: dict[int, str] = {}
# Preferred provider per chat, set via /provider, used as the default for /new
_chat_preferred_provider: dict[int, str] = {}


def is_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN)


def markdown_to_telegram_html(text: str) -> str:
    """Convert standard LLM Markdown into clean Telegram-compatible HTML.
    
    Supports:
    - Code blocks (```lang ... ``` -> <pre><code class="language-lang">...</code></pre>)
    - Inline code (`code` -> <code>...</code>)
    - Bold (**bold** or __bold__ -> <b>bold</b>)
    - Italic (*italic* or _italic_ -> <i>italic</i>)
    - Strikethrough (~~text~~ -> <s>text</s>)
    - Markdown links ([text](url) -> <a href="url">text</a>)
    - Blockquotes (> text -> <blockquote>text</blockquote>)
    - Headers (# Header -> <b>Header</b>)
    """
    if not text:
        return ""

    # 1. Extract and protect code blocks
    code_blocks: list[str] = []
    def _save_code_block(match):
        lang = match.group(1) or ""
        code_content = match.group(2)
        escaped_code = html.escape(code_content)
        idx = len(code_blocks)
        if lang:
            tag = f'<pre><code class="language-{html.escape(lang)}">{escaped_code}</code></pre>'
        else:
            tag = f'<pre>{escaped_code}</pre>'
        code_blocks.append(tag)
        return f"%%CODEBLOCK{idx}%%"

    # Match ```lang\ncode```
    text = re.sub(r'```(?:([a-zA-Z0-9_\-\+]+)\n)?(.*?)```', _save_code_block, text, flags=re.DOTALL)

    # 2. Extract and protect inline code
    inline_codes: list[str] = []
    def _save_inline_code(match):
        code_content = match.group(1)
        escaped = html.escape(code_content)
        idx = len(inline_codes)
        inline_codes.append(f"<code>{escaped}</code>")
        return f"%%INLINECODE{idx}%%"

    text = re.sub(r'`([^`\n]+)`', _save_inline_code, text)

    # 3. Escape general HTML characters
    text = html.escape(text)

    # 4. Headers: # Header -> <b>Header</b>
    text = re.sub(r'^(?:#{1,6})\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)

    # 5. Bold: **text** or __text__ -> <b>text</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)

    # 6. Italic: *text* or _text_ -> <i>text</i> (avoiding underscore in words)
    text = re.sub(r'(?<!\w)\*([^\*\n]+?)\*(?!\w)', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_([^_\n]+?)_(?!\w)', r'<i>\1</i>', text)

    # 7. Strikethrough: ~~text~~ -> <s>text</s>
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)

    # 8. Links: [text](url) -> <a href="url">text</a>
    # Note: text and url are html-escaped at this stage (&amp;, &quot;, etc.)
    text = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'<a href="\2">\1</a>', text)

    # 9. Restore code blocks & inline code
    for idx, tag in enumerate(inline_codes):
        text = text.replace(f"%%INLINECODE{idx}%%", tag)

    for idx, tag in enumerate(code_blocks):
        text = text.replace(f"%%CODEBLOCK{idx}%%", tag)

    return text


def split_message_chunks(text: str, max_chunk_size: int = 4000) -> list[str]:
    """Split long text into chunks, trying to break on newlines/paragraphs."""
    if len(text) <= max_chunk_size:
        return [text] if text else ["(empty)"]

    chunks = []
    lines = text.splitlines(keepends=True)
    cur = []
    cur_len = 0

    for line in lines:
        if cur_len + len(line) > max_chunk_size and cur:
            chunks.append("".join(cur))
            cur = [line]
            cur_len = len(line)
        else:
            cur.append(line)
            cur_len += len(line)

    if cur:
        chunks.append("".join(cur))
    return chunks


# Lock per chat so turns don't overlap
_chat_locks: dict[int, asyncio.Lock] = {}


def otp_chat_id() -> int | None:
    """Resolve which Telegram chat receives web-login one-time codes.

    TELEGRAM_OTP_CHAT_ID wins if set; otherwise, if exactly one user is
    whitelisted via TELEGRAM_ALLOWED_USERS, use that — a user's private chat
    id with the bot is the same as their Telegram user id. Returns None
    (OTP login unavailable) if neither resolves unambiguously.
    """
    if TELEGRAM_OTP_CHAT_ID:
        try:
            return int(TELEGRAM_OTP_CHAT_ID)
        except ValueError:
            return None
    if len(TELEGRAM_ALLOWED_USERS) == 1:
        return TELEGRAM_ALLOWED_USERS[0]
    return None


def otp_enabled() -> bool:
    return is_configured() and otp_chat_id() is not None


async def send_otp_code(code: str) -> bool:
    """Send a one-time web-login code to the configured OTP chat.

    Returns True if the Telegram API call succeeded, False otherwise (bot not
    configured, no resolvable chat, or the send itself failed).
    """
    chat_id = otp_chat_id()
    if not chat_id:
        return False
    api = TelegramAPI(TELEGRAM_BOT_TOKEN)
    try:
        await api.send_message(
            chat_id,
            f"🔐 *ds-agent login code:* `{code}`\n\nExpires in 5 minutes. Ignore this if it wasn't you.",
        )
        return True
    except Exception:
        logger.exception("Failed to send web-login OTP via Telegram")
        return False


class TelegramAPI:
    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"

    def _request(self, method: str, data: dict | None = None, files: dict | None = None, retries: int = 1) -> dict:
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

        # This host sees frequent transient network blips to api.telegram.org
        # (connection reset, TLS handshake timeout — see docs/debug_notes.md).
        # A single blip on a one-shot call like sendMessage used to be dropped
        # silently with no retry, which could leave the user with literally no
        # response even when the agent turn itself worked fine.
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=35) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                last_err = e
                if attempt < retries:
                    time.sleep(1.5)
        logger.error("Telegram API %s failed after %d attempt(s): %s", method, retries + 1, last_err)
        return {"ok": False, "description": str(last_err)}

    async def call(self, method: str, data: dict | None = None, files: dict | None = None, retries: int = 1) -> dict:
        return await asyncio.to_thread(self._request, method, data, files, retries)

    async def send_message(self, chat_id: int, text: str, parse_mode: str = "HTML", reply_markup: dict | None = None) -> dict:
        # Convert Markdown to HTML if parse_mode is HTML or Markdown
        formatted_text = text
        if parse_mode == "HTML":
            formatted_text = markdown_to_telegram_html(text)
        elif parse_mode == "Markdown":
            # For backward compatibility if someone passes raw Markdown
            formatted_text = markdown_to_telegram_html(text)
            parse_mode = "HTML"

        chunks = split_message_chunks(formatted_text, max_chunk_size=4000)
        res = {}
        for idx, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            # Only attach reply_markup to the last chunk
            if reply_markup and idx == len(chunks) - 1:
                payload["reply_markup"] = reply_markup

            res = await self.call("sendMessage", payload)
            if not res.get("ok") and parse_mode:
                # If HTML/Markdown parse fails for any edge case, fallback to plain raw text chunk
                raw_chunk = split_message_chunks(text, max_chunk_size=4000)[idx] if idx < len(split_message_chunks(text, max_chunk_size=4000)) else chunk
                payload["text"] = raw_chunk
                payload.pop("parse_mode", None)
                res = await self.call("sendMessage", payload)
        return res

    async def answer_callback_query(self, callback_query_id: str, text: str = "") -> dict:
        return await self.call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})

    async def get_file_download_url(self, file_id: str) -> str | None:
        res = await self.call("getFile", {"file_id": file_id})
        if res.get("ok") and "result" in res:
            file_path = res["result"].get("file_path")
            if file_path:
                return f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        return None

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


def _normalize_model_query(text: str) -> str:
    """Lowercase and collapse punctuation/whitespace to single spaces.

    Lets a query like "gemini 3.7" match an id like "google/gemini-3.7-pro"
    (or "gemini_3_7") regardless of which separator the catalog uses.
    """
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _model_matches_query(query: str, model: dict) -> bool:
    """True if every whitespace-separated token in `query` appears in the
    model's id or label (order-independent, punctuation-insensitive).

    Splitting into tokens (rather than one substring match) is what makes
    multi-word queries like "gemini 3.7" work — a single `in` check against
    "google/gemini-2.5-pro" would need the literal substring "gemini 3.7",
    which never occurs even when the model is a good match.
    """
    haystack = _normalize_model_query(f"{model['id']} {model['label']}")
    tokens = _normalize_model_query(query).split()
    return all(tok in haystack for tok in tokens)


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

    # Find default provider: chat's /provider pick, else openrouter, else whatever's configured
    provs = crypto.list_providers()
    preferred = _chat_preferred_provider.get(chat_id)
    if preferred and preferred in provs:
        provider = preferred
    else:
        provider = "openrouter" if "openrouter" in provs else (provs[0] if provs else "openrouter")
    model = "anthropic/claude-sonnet-4.5" if provider == "openrouter" else "claude-sonnet-4-5"
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
            "• `/provider [name]` - Show/pick the BYOK provider used as the default for `/new`\n"
            "• `/sessions` - List existing sessions with 1-click switch buttons\n"
            "• `/switch <id>` - Switch to an existing session\n"
            "• `/compact` - Compact context window\n"
            "• `/stop` - Interrupt the current running turn\n"
            "• `/status` - Show current active session info\n\n"
            "Send any prompt or question to start working with the agent!"
        )
        await api.send_message(chat_id, msg)
        return

    if cmd == "/models":
        # Check if user requested a specific category/provider or page
        # e.g. /models [provider/query/page] — join the remaining words so
        # multi-word queries like "gemini 3.7" aren't truncated to "gemini".
        query = " ".join(parts[1:]).strip() if len(parts) > 1 else ""

        # Fetch live OpenRouter models if openrouter key available, otherwise use curated
        live_or_models = await model_catalog.openrouter_live_models()
        buttons: list[list[dict]] = []

        if live_or_models:
            # Filter if search query provided
            models_to_show = live_or_models
            if query:
                models_to_show = [m for m in live_or_models if _model_matches_query(query, m)]

            total_found = len(models_to_show)
            # Display top 15 results with inline keyboard buttons for 1-click creation
            display_slice = models_to_show[:15]

            lines = [f"🌐 *All Available Models* ({total_found} total):"]
            if query:
                lines.append(f"_Filter: '{query}'_")
            lines.append("\n_Click a button below or type `/new <model_id>`:_")

            for m in display_slice:
                tag_str = f" [{m['tag']}]" if m.get("tag") and m["tag"] != "default" else ""
                short_label = m['label'][:35]
                # Each button has callback_data with model id
                buttons.append([
                    {"text": f"✨ {short_label}{tag_str}", "callback_data": f"new:{m['id']}"}
                ])

            if total_found > 15:
                lines.append(f"\n_Showing first 15 of {total_found} models. Filter with `/models <name>` (e.g. `/models claude`, `/models gpt`, `/models llama`, `/models deepseek`)._")
        else:
            # Fallback to curated catalog
            lines = ["*Available Models (Curated):*"]
            for prov, mlist in model_catalog.CURATED.items():
                if prov == "custom":
                    continue
                for m in mlist:
                    if m["id"]:
                        buttons.append([
                            {"text": f"{prov.upper()}: {m['label']}", "callback_data": f"new:{m['id']}"}
                        ])

        reply_markup = {"inline_keyboard": buttons} if buttons else None
        await api.send_message(chat_id, "\n".join(lines), reply_markup=reply_markup)
        return

    if cmd == "/provider":
        provs = crypto.list_providers()
        if not provs:
            await api.send_message(chat_id, "No providers configured yet. Add BYOK keys in the web UI (Settings → BYOK keys).")
            return

        if len(parts) > 1:
            target = parts[1].strip().lower()
            if target not in provs:
                configured = ", ".join(f"`{p}`" for p in provs)
                await api.send_message(chat_id, f"❌ Provider `{target}` is not configured. Configured providers: {configured}")
                return
            _chat_preferred_provider[chat_id] = target
            await api.send_message(chat_id, f"✅ Default provider for `/new` set to `{target}`.")
            return

        current = _chat_preferred_provider.get(chat_id)
        lines = ["*Configured Providers:*"]
        buttons = []
        for p in provs:
            marker = " 👈 *(current default)*" if p == current else ""
            lines.append(f"• `{p}`{marker}")
            buttons.append([{"text": f"Use {p}", "callback_data": f"setprov:{p}"}])
        lines.append("\nTap a button, or use `/provider <name>` to set the default used by `/new`.")
        await api.send_message(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": buttons})
        return

    if cmd == "/new":
        specified_model = parts[1].strip() if len(parts) > 1 else ""
        provs = crypto.list_providers()

        preferred = _chat_preferred_provider.get(chat_id)
        if preferred and preferred in provs:
            provider = preferred
        else:
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
            model = "anthropic/claude-sonnet-4.5" if provider == "openrouter" else "claude-sonnet-4-5"

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
        buttons: list[list[dict]] = []
        for s in sess_list:
            is_current = s["id"] == cur_id
            active_marker = " 👈 *(current)*" if is_current else ""
            lines.append(f"• `{s['id'][:8]}` - *{s['title']}* ({s['model']}){active_marker}")
            btn_prefix = "✅ " if is_current else "🔀 "
            btn_label = f"{btn_prefix}{s['title']} ({s['id'][:8]})"[:60]
            buttons.append([{"text": btn_label, "callback_data": f"switch:{s['id']}"}])
        lines.append("\nTap a button below, or use `/switch <id>`")
        await api.send_message(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": buttons})
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
        else:
            await api.send_message(chat_id, "❌ No active session found. Use `/new` or select a model to start a session.")
        return


def strip_thinking_and_internal_tags(text: str) -> str:
    """Strip out any raw internal thinking/scratchpad tags like <think>...</think> or <thought>...</thought>."""
    if not text:
        return ""
    # Strip <think>...</think> or <thought>...</thought> blocks (common in DeepSeek R1 / reasoning models)
    cleaned = re.sub(r'<think(?:ing)?>.*?</think(?:ing)?>', '', text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<thought>.*?</thought>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    # Strip unclosed <think> if any
    cleaned = re.sub(r'<think(?:ing)?>.*', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


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

        # Subscribe BEFORE sending: if a web UI tab already has this session
        # open, the shared engine is already running, and send_user_message()
        # can be picked up and answered before a subscription registered
        # afterward would exist — we'd then wait forever for a reply we
        # already missed. See sessions.subscribe()'s docstring.
        sub_q = sessions.subscribe(active)
        await sessions.send_user_message(active, user_text)

        turn_texts: list[str] = []
        last_typing_time = asyncio.get_event_loop().time()

        # Explicit generator + aclose() (rather than a bare `async for`) so the
        # subscriber queue is removed deterministically on return/exception,
        # not whenever GC gets to it — this is a shared fan-out subscription
        # now, not a private reader, so a lingering unread queue would leak.
        events = sessions.stream_from(active, sub_q)
        try:
            async for frame in events:
                ftype = frame.get("type")

                # Heartbeat / typing action
                now = asyncio.get_event_loop().time()
                if now - last_typing_time > 4.0:
                    await api.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
                    last_typing_time = now

                if ftype == "assistant":
                    # Ignore thinking blocks completely; collect only final assistant text
                    content = frame.get("content") or []
                    block_texts = []
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        b_type = b.get("type")
                        # Explicitly exclude thinking blocks
                        if b_type in ("thinking", "thought"):
                            continue
                        if b_type == "text" and b.get("text"):
                            cleaned_t = strip_thinking_and_internal_tags(b.get("text", ""))
                            if cleaned_t:
                                block_texts.append(cleaned_t)
                    if block_texts:
                        turn_texts.append("\n\n".join(block_texts))

                elif ftype == "error":
                    await api.send_message(chat_id, f"⚠️ *Agent Error:* {frame.get('message')}")
                    return

                elif ftype == "result":
                    # We send only the final end result text from the agent
                    # If multiple assistant text chunks were produced (e.g. across intermediate tool steps),
                    # the last chunk represents the final conclusion/result to the user.
                    if turn_texts:
                        final_text = turn_texts[-1].strip()
                    else:
                        final_text = ""

                    if final_text:
                        await api.send_message(chat_id, final_text)

                    # Check for created plots / artifacts in session workspace
                    sent_artifact = False
                    ws_dir = Path(active.db_row["workspace"])
                    if ws_dir.exists():
                        # Find recently modified image files
                        for img in ws_dir.glob("*.png"):
                            try:
                                if img.stat().st_mtime >= (now - 120):  # created/modified in last 2m
                                    await api.send_photo(chat_id, img.read_bytes(), caption=img.name)
                                    sent_artifact = True
                            except Exception:
                                pass

                    # The turn completed but produced neither text nor an
                    # artifact — e.g. the watchdog (sessions.stream_events)
                    # interrupted a hung model call and got back an empty
                    # result. Without this, the user sees total silence and
                    # has no way to tell the turn even ran (see
                    # docs/debug_notes.md "Telegram silent-empty-turn bug").
                    if not final_text and not sent_artifact:
                        await api.send_message(
                            chat_id,
                            "⚠️ The agent turn finished without producing a reply "
                            "(the model may be unrecognized/unresponsive — check "
                            "`/status` or try `/new` with a different model).",
                        )

                    return
        except Exception as e:
            logger.error("Error during Telegram agent turn: %s", e)
            await api.send_message(chat_id, f"❌ Turn error: {e}")
        finally:
            await events.aclose()


async def _handle_callback_query(api: TelegramAPI, cb: dict) -> None:
    cb_id = cb.get("id", "")
    data = cb.get("data", "")
    message = cb.get("message", {})
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    from_user = cb.get("from", {})
    user_id = from_user.get("id")

    if not chat_id:
        return

    # Access control
    if TELEGRAM_ALLOWED_USERS and user_id not in TELEGRAM_ALLOWED_USERS:
        await api.answer_callback_query(cb_id, "⛔ Access denied.")
        return

    if data.startswith("new:"):
        model = data[4:].strip()
        await api.answer_callback_query(cb_id, f"Creating session with {model[:25]}...")
        await _handle_command(api, chat_id, f"/new {model}")
    elif data.startswith("setprov:"):
        provider = data[8:].strip()
        await api.answer_callback_query(cb_id, f"Default provider set to {provider}")
        await _handle_command(api, chat_id, f"/provider {provider}")
    elif data.startswith("switch:"):
        sid = data[7:].strip()
        await api.answer_callback_query(cb_id, f"Switching to {sid[:8]}...")
        await _handle_command(api, chat_id, f"/switch {sid}")
    else:
        await api.answer_callback_query(cb_id)


async def _handle_document_upload(api: TelegramAPI, chat_id: int, user_id: int, doc: dict, caption: str = "") -> None:
    """Download an uploaded file (e.g. CSV, JSON, parquet, py) and save it to the active session workspace."""
    sid = _ensure_session_for_chat(chat_id)
    row = db.get_session(sid)
    if not row:
        await api.send_message(chat_id, "❌ Active session not found.")
        return

    ws_dir = Path(row["workspace"])
    ws_dir.mkdir(parents=True, exist_ok=True)

    file_id = doc.get("file_id")
    raw_name = doc.get("file_name") or f"uploaded_file_{os.urandom(4).hex()}"
    file_name = Path(raw_name).name  # sanitize

    # Check file size (Telegram bot API direct download limit is 20MB)
    file_size = doc.get("file_size", 0)
    if file_size > 20 * 1024 * 1024:
        await api.send_message(chat_id, "⚠️ File exceeds 20MB limit for Telegram bots.")
        return

    await api.call("sendChatAction", {"chat_id": chat_id, "action": "upload_document"})

    url = await api.get_file_download_url(file_id)
    if not url:
        await api.send_message(chat_id, f"❌ Failed to obtain download link for `{file_name}`.")
        return

    dest = ws_dir / file_name

    def _download():
        req = urllib.request.Request(url, headers={"User-Agent": "ds-agent/0.1"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            dest.write_bytes(resp.read())

    try:
        await asyncio.to_thread(_download)
    except Exception as e:
        logger.error("Failed downloading Telegram file %s: %s", file_name, e)
        await api.send_message(chat_id, f"❌ Download error: {e}")
        return

    size_kb = dest.stat().st_size / 1024
    size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb/1024:.2f} MB"

    notification = (
        f"📁 *Uploaded file saved to workspace:*\n"
        f"• *File:* `{file_name}` ({size_str})\n"
        f"• *Session:* `{sid[:8]}`\n"
        f"• *Path in agent:* `{file_name}`"
    )
    await api.send_message(chat_id, notification)

    # If the user included a caption with the file, send it as the prompt to analyze the file
    prompt = caption.strip()
    if prompt:
        user_prompt = f"I uploaded `{file_name}` to your workspace. {prompt}"
        asyncio.create_task(_run_agent_turn_for_telegram(api, chat_id, user_prompt))
    else:
        user_prompt = f"I have uploaded `{file_name}` to your workspace. Please examine this file and summarize its structure and contents."
        asyncio.create_task(_run_agent_turn_for_telegram(api, chat_id, user_prompt))


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
            # retries=0: the outer while loop already retries on failure, and
            # this is a 30s long-poll — stacking a blocking retry would just
            # double the delay before the loop notices and retries itself.
            updates_res = await api.call("getUpdates", {"offset": offset, "timeout": 30, "limit": 20}, retries=0)
            if not updates_res.get("ok"):
                await asyncio.sleep(5)
                continue

            for u in updates_res.get("result", []):
                offset = max(offset, u["update_id"] + 1)

                # Handle inline keyboard callback clicks (e.g. 1-click model selection)
                cb = u.get("callback_query")
                if cb:
                    asyncio.create_task(_handle_callback_query(api, cb))
                    continue

                msg = u.get("message")
                if not msg:
                    continue

                chat = msg.get("chat", {})
                chat_id = chat.get("id")
                from_user = msg.get("from", {})
                user_id = from_user.get("id")
                text = msg.get("text", "")
                caption = msg.get("caption", "")
                doc = msg.get("document")

                if not chat_id:
                    continue

                # Access control check
                if TELEGRAM_ALLOWED_USERS and user_id not in TELEGRAM_ALLOWED_USERS:
                    await api.send_message(
                        chat_id,
                        f"⛔ Access denied. Your Telegram user ID is `{user_id}`. Add it to `TELEGRAM_ALLOWED_USERS`."
                    )
                    continue

                # Handle document / file upload (CSV, JSON, data files, etc.)
                if doc:
                    asyncio.create_task(_handle_document_upload(api, chat_id, user_id, doc, caption))
                    continue

                if not text:
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
