"""Just open a WS briefly to trigger session start, then we can inspect
what MCPs were loaded by looking at the rendered .mcp.json and the spawned
subprocesses.
"""
import asyncio
import json
import sys
import websockets
sys.stdout.reconfigure(line_buffering=True, write_through=True)


async def run(sid):
    uri = f"ws://127.0.0.1:8765/ws/sessions/{sid}"
    try:
        async with websockets.connect(uri, max_size=10_000_000) as ws:
            ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
            print(f"READY: model={ready.get('model')} provider={ready.get('provider')}", flush=True)
            print(f"workspace: {ready.get('workspace')}", flush=True)
            # Send a small prompt asking what tools are available
            await ws.send(json.dumps({
                "type": "user",
                "text": "List every tool you have access to, in a numbered list. Just the tool names, one per line."
            }))
            # Wait for result
            try:
                async for raw in ws:
                    f = json.loads(raw)
                    t = f.get("type")
                    if t == "result":
                        print(f"RESULT: turns={f.get('num_turns')}", flush=True)
                        print(f"result_text: {f.get('result','')!r}", flush=True)
                        break
                    elif t == "assistant":
                        msg = f.get("content", [])
                        for b in msg:
                            if b.get("type") == "text" and b.get("text"):
                                print(f"TEXT: {b['text']!r}", flush=True)
                            elif b.get("type") == "tool_use":
                                print(f"TOOL_USE: {b.get('name')}", flush=True)
            except asyncio.TimeoutError:
                print("Timeout waiting for result", flush=True)
    except Exception as e:
        print(f"WS error: {e}", flush=True)


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
