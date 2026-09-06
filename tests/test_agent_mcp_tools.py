"""Spawns agent_mcp.py over real MCP stdio (like the claude CLI does) and
exercises every tool: list_sessions, search_other_sessions, remember, recall,
forget. No live server/SDK client needed.
Run with: PYTHONPATH=src python tests/test_agent_mcp_tools.py
"""
import asyncio, os, sys, tempfile, json
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")

tmp = tempfile.mkdtemp()
os.environ["CODING_AGENT_HOME"] = tmp
os.environ["PYTHONPATH"] = SRC

sys.path.insert(0, SRC)
from ds_agent import db, sessions
db.init()
row = sessions.create(provider="openrouter", model="anthropic/claude-sonnet-4.5", title="Test session")
sid = row["id"]
db.add_memory("pre-existing memory for mcp stdio test", tags="test")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ds_agent.agent_mcp"],
        cwd=row["workspace"],  # cwd = session workspace, exactly like the real CLI spawns it
        env={**os.environ, "PYTHONPATH": SRC, "CODING_AGENT_HOME": tmp},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            names = sorted(t.name for t in tools)
            print("tools:", names)
            assert names == sorted(["list_sessions", "search_other_sessions", "get_session_summary", "remember", "recall", "forget"]), names

            r = await session.call_tool("list_sessions", {})
            data = json.loads(r.content[0].text)
            assert any(s["id"] == sid for s in data), data
            print("list_sessions via stdio: OK")

            r = await session.call_tool("recall", {})
            data = json.loads(r.content[0].text)
            assert any("pre-existing memory" in m["text"] for m in data), data
            print("recall via stdio: OK ->", data)

            r = await session.call_tool("remember", {"text": "saved from within the mcp tool call", "tags": "unit-test"})
            data = json.loads(r.content[0].text)
            assert data["ok"] and data["id"]
            print("remember via stdio: OK ->", data)

            r = await session.call_tool("recall", {"query": "within the mcp"})
            data = json.loads(r.content[0].text)
            assert len(data) == 1 and "within the mcp tool call" in data[0]["text"]
            mem_id = data[0]["id"]
            print("recall with query filter via stdio: OK")

            r = await session.call_tool("forget", {"memory_id": mem_id})
            data = json.loads(r.content[0].text)
            assert data["ok"] is True
            print("forget via stdio: OK")

            r = await session.call_tool("search_other_sessions", {"query": "nonexistent-query-xyz"})
            data = json.loads(r.content[0].text)
            assert data == []
            print("search_other_sessions via stdio (empty result): OK")

    print("\nALL MCP STDIO CHECKS PASSED")

asyncio.run(main())
