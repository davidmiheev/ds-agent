"""Provider adapters: turn a stored BYOK key into env vars for the claude CLI subprocess."""
from __future__ import annotations

def env_for(provider: str, key: str, base_url: str | None = None, model: str | None = None) -> dict[str, str]:
    """Return the env dict to inject into the claude subprocess for this provider.

    The ANTHROPIC_API_KEY="" (empty string, not unset) is critical when using a
    non-Anthropic base URL — otherwise the CLI silently falls back to first-party auth.
    """
    e: dict[str, str] = {}
    if provider == "anthropic":
        e["ANTHROPIC_API_KEY"] = key
    elif provider == "openrouter":
        e["ANTHROPIC_BASE_URL"]   = base_url or "https://openrouter.ai/api"
        e["ANTHROPIC_AUTH_TOKEN"] = key
        e["ANTHROPIC_API_KEY"]    = ""
        e["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
    else:
        # generic gateway: user provides base_url
        e["ANTHROPIC_BASE_URL"]   = base_url or ""
        e["ANTHROPIC_AUTH_TOKEN"] = key
        e["ANTHROPIC_API_KEY"]    = ""
    if model:
        e["ANTHROPIC_MODEL"] = model
    # Dedicated data-science interpreter (see scripts/install_ds_env.sh).
    # The ds_mcp server and the agent's shell both use this for data work.
    from . import core
    e["DS_PYTHON"] = str(core.DS_PYTHON)
    return e
