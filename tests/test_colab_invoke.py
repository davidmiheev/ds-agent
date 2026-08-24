"""Invoke the colab MCP tool from the agent and capture the error path.

We have fake OAuth tokens, so the call will fail. But the failure path proves
the full tool-call loop: client → claude CLI → colab-mcp subprocess → back.
"""
import asyncio, json, sys, websockets
sys.stdout.reconfigure(line_buffering=True, write_through=True)


async def run(sid):
    uri = f"ws://127.0.0.1:8765/ws/sessions/{sid}"
    async with websockets.connect(uri, max_size=10_000_000) as ws:
        ready = json.loads(await ws.recv())
        print(f"READY", flush=True)

        await ws.send(json.dumps({
            "type": "user",
            "text": "Try to call the open_colab_browser_connection tool. Report what happens (the result, any error). Then briefly summarize what MCP tools you can see available to you right now."
        }))

        try:
            async for raw in ws:
                f = json.loads(raw)
                t = f.get("type")
                if t == "result":
                    print(f"\nRESULT: turns={f.get('num_turns')}", flush=True)
                    print(f"result_text: {f.get('result','')!r}", flush=True)
                    break
                elif t == "assistant":
                    for b in f.get("content", []):
                        if b.get("type") == "text" and b.get("text"):
                            print(f"TEXT: {b['text']!r}", flush=True)
                        elif b.get("type") == "tool_use":
                            print(f"TOOL_USE: {b.get('name')} input={json.dumps(b.get('input', {}))[:200]}", flush=True)
                elif t == "user":
                    for b in f.get("content", []):
                        if b.get("type") == "tool_result":
                            c = b.get("content")
                            if isinstance(c, list):
                                for sub in c:
                                    if isinstance(sub, dict) and sub.get("type") == "text":
                                        print(f"TOOL_RESULT: {sub.get('text','')[:400]!r}", flush=True)
                            elif isinstance(c, str):
                                print(f"TOOL_RESULT str: {c[:400]!r}", flush=True)
        except Exception as e:
            print(f"ERR: {e}", flush=True)


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1]))
