"""Curated model catalog for the picker + live OpenRouter enrichment.

Each entry: {"id": "anthropic/claude-sonnet-4-5", "label": "...", "tag": "smartest|fastest|cheap|default", "ctx": 200000}
"""
from __future__ import annotations
import asyncio
import json
import urllib.request
from typing import Any

# A small default for each provider so the picker has *something* even when
# offline. Order = shown order in dropdown.
CURATED: dict[str, list[dict]] = {
    "openrouter": [
        {"id": "anthropic/claude-sonnet-4-5",       "label": "Claude Sonnet 4.5 (smartest general)",   "tag": "default",  "ctx": 1_000_000},
        {"id": "anthropic/claude-haiku-4-5",        "label": "Claude Haiku 4.5 (fast, cheap)",         "tag": "fastest",  "ctx": 200_000},
        {"id": "openai/gpt-5",                      "label": "GPT-5",                                  "tag": "smartest", "ctx": 400_000},
        {"id": "openai/gpt-5-mini",                 "label": "GPT-5 mini (fast)",                      "tag": "fastest",  "ctx": 400_000},
        {"id": "openai/o3",                         "label": "o3 (deep reasoning)",                    "tag": "reasoning","ctx": 200_000},
        {"id": "openai/o4-mini",                    "label": "o4-mini (cheap reasoning)",              "tag": "reasoning","ctx": 200_000},
        {"id": "google/gemini-2.5-pro",             "label": "Gemini 2.5 Pro",                         "tag": "smartest", "ctx": 1_000_000},
        {"id": "google/gemini-2.5-flash",           "label": "Gemini 2.5 Flash (fast)",                 "tag": "fastest",  "ctx": 1_000_000},
        {"id": "deepseek/deepseek-chat",            "label": "DeepSeek V3 (cheap)",                    "tag": "cheap",    "ctx": 64_000},
        {"id": "meta-llama/llama-4-maverick",       "label": "Llama 4 Maverick",                       "tag": "default",  "ctx": 1_000_000},
        {"id": "qwen/qwen-2.5-72b-instruct",        "label": "Qwen 2.5 72B (cheap)",                   "tag": "cheap",    "ctx": 32_000},
    ],
    "anthropic": [
        {"id": "claude-sonnet-4-5",                 "label": "Claude Sonnet 4.5",  "tag": "default",  "ctx": 1_000_000},
        {"id": "claude-haiku-4-5",                  "label": "Claude Haiku 4.5",   "tag": "fastest",  "ctx": 200_000},
    ],
    "minimax": [
        # Placeholder — user fills in the real base URL + model name.
        {"id": "MiniMax-model",                                "label": "MiniMax default (placeholder)", "tag": "default", "ctx": 32_000},
    ],
    "custom": [
        # Free-form; the UI lets the user type any model id
        {"id": "", "label": "(type custom model id below)", "tag": "default", "ctx": 0},
    ],
}


async def openrouter_live_models(api_key: str | None = None, timeout: float = 4.0) -> list[dict] | None:
    """Hit OpenRouter's /api/v1/models and return a sorted list.

    Returns None on any error so the caller can fall back to the curated list.
    Each result: {id, label, tag, ctx}. `ctx` is context_length.
    """
    def _fetch():
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"User-Agent": "ds-agent/0.1"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    try:
        data = await asyncio.to_thread(_fetch)
    except Exception:
        return None
    items = data.get("data", [])
    out: list[dict] = []
    for m in items:
        mid = m.get("id", "")
        if not mid:
            continue
        name = m.get("name", mid)
        ctx = int(m.get("context_length") or 0)
        # crude tag: cheap if pricing < $0.5/M, reasoning if it's an o-series,
        # otherwise default
        pricing = m.get("pricing", {}) or {}
        prompt = float(pricing.get("prompt") or 0) * 1_000_000
        tag = "default"
        if mid.startswith(("openai/o", "deepseek/deepseek-r", "anthropic/claude-3-7")):
            tag = "reasoning"
        elif prompt and prompt < 0.5:
            tag = "cheap"
        elif "haiku" in mid or "flash" in mid or "mini" in mid:
            tag = "fastest"
        out.append({"id": mid, "label": name, "tag": tag, "ctx": ctx})
    # sort: anthropic first, then by name
    out.sort(key=lambda x: (not x["id"].startswith("anthropic/"), x["label"]))
    return out
