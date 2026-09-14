"""Smoke test for the vast-mcp server: spawns it as a real stdio subprocess
and verifies the tool list. No Vast.ai account/API key needed for this.
Run:  PYTHONPATH=src python tests/test_vast_mcp_server.py
"""
import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "vast_mcp.server"],
        cwd=str(root),
        env={**os.environ, "PYTHONPATH": str(root / "src")},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"server: {init.server_info.name} {init.server_info.version}")
            tools = (await session.list_tools()).tools
            names = sorted(t.name for t in tools)
            print(f"tools ({len(names)}): {names}")
            expected = sorted([
                "vast_search_offers", "vast_new", "vast_status", "vast_sessions",
                "vast_execute", "vast_upload", "vast_install", "vast_stop", "vast_destroy",
            ])
            assert names == expected, (names, expected)
            print("tool list matches expected: OK")


if __name__ == "__main__":
    asyncio.run(main())
