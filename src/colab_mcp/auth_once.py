#!/usr/bin/env python3
"""One-time interactive Google OAuth for the Colab MCP server.

Run this once (on the box where the agent runs — works over SSH too):

    src/colab_mcp/.venv/bin/python src/colab_mcp/auth_once.py

It prints an authorization URL. Open it in ANY browser (your laptop is fine —
nothing needs a browser on this host), sign in with the Google account you
want to use for Colab, and paste back the authorization code Google shows.

The resulting token (with refresh token) is saved to
~/.config/colab-cli/token.json and auto-refreshes silently forever after,
so the colab MCP server never needs interactive login again.

This is just a thin wrapper around colab_cli's own credential logic, so it
behaves exactly like the official `colab` CLI's first run.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make colab_cli importable when run from the repo checkout.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from colab_cli.auth import _get_google_auth_credentials, TOKEN_CONFIG_PATH  # noqa: E402


def main() -> int:
    # If a valid token already exists, this is a no-op that just loads it.
    if os.path.exists(TOKEN_CONFIG_PATH):
        print(f"Found existing token at {TOKEN_CONFIG_PATH} — validating/refreshing...")

    creds = _get_google_auth_credentials("")  # "" → use the inlined public client config

    print(f"\nOK — authenticated as {getattr(creds, 'token', '') and 'google user'}")
    print(f"Token saved to: {TOKEN_CONFIG_PATH}")
    print("The colab MCP server will now work without any further login.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
