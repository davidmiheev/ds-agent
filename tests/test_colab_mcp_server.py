"""Smoke test for the colab-mcp server.

Spawns the server as a stdio subprocess and verifies the tool list is
exposed correctly. Does not touch Google APIs, so no auth required.

Run:  .venv-313/bin/python tests/test_colab_mcp_server.py
"""
import asyncio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main():
    import sys
    here = __import__("pathlib").Path(__file__).parent
    root = here.parent
    python = root / ".venv-313" / "bin" / "python"
    if not python.exists():
        python = root / "src" / "colab_mcp" / ".venv" / "bin" / "python"
    params = StdioServerParameters(
        command=str(python),
        args=["-m", "colab_mcp.colab_server"],
        cwd=str(root),
        env={**__import__("os").environ, "PYTHONPATH": str(root / "src")},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            print(f"server: {init.serverInfo.name} {init.serverInfo.version}")
            tools = await s.list_tools()
            print(f"tools: {len(tools.tools)}")
            for t in tools.tools:
                print(f"  - {t.name}: {t.description[:60]}...")

            # Verify colab_auth returns a structured auth URL when no code given
            print()
            print("--- colab_auth (no code) ---")
            r = await s.call_tool("colab_auth", {})
            for c in r.content:
                if hasattr(c, "text"):
                    print(c.text[:300])


if __name__ == "__main__":
    asyncio.run(main())
