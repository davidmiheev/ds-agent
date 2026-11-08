"""Request auto-correction for Kaggle MCP tools whose schemas have footguns.

Split out of kaggle_mcp.py so code that talks to Kaggle without the `mcp`
package (the batch runner's command-line clients) applies the same fixes.
kaggle_mcp.py re-exports these names. See docs/debug_notes.md (2026-09-06
kaggle entries) for why each correction exists.
"""
from __future__ import annotations

from typing import Any

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
