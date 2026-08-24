"""Dump every frame from the WS so we can see the exact structure."""
import asyncio
import json
import sys
import websockets
sys.stdout.reconfigure(line_buffering=True, write_through=True)

async def run(sid: str, prompt: str):
    uri = f"ws://127.0.0.1:8765/ws/sessions/{sid}"
    async with websockets.connect(uri, max_size=50_000_000) as ws:
        ready = json.loads(await ws.recv())
        print(f"GREET: {ready}", flush=True)

        await ws.send(json.dumps({"type": "user", "text": prompt}))

        try:
            async for raw in ws:
                f = json.loads(raw)
                t = f.get("type")
                if t == "result":
                    print(f"RESULT: cost=${f.get('total_cost_usd')} turns={f.get('num_turns')} result={f.get('result')!r}", flush=True)
                    print(f"  usage: {f.get('usage')}", flush=True)
                    break
                elif t == "assistant":
                    msg = f.get("message", {})
                    for b in msg.get("content", []):
                        if b.get("type") == "text":
                            print(f"ASSIST TEXT: {b.get('text','')!r}", flush=True)
                        elif b.get("type") == "thinking":
                            print(f"THINK: {b.get('thinking','')[:200]!r}...", flush=True)
                        elif b.get("type") == "tool_use":
                            print(f"TOOL_USE: {b.get('name')} {json.dumps(b.get('input', {}))[:300]}", flush=True)
                        else:
                            print(f"ASSIST BLOCK: {b}", flush=True)
                elif t == "user":
                    msg = f.get("message", {})
                    for b in msg.get("content", []):
                        if b.get("type") == "tool_result":
                            c = b.get("content")
                            if isinstance(c, list):
                                for sub in c:
                                    if sub.get("type") == "text":
                                        print(f"TOOL_RESULT text: {sub.get('text','')[:300]!r}", flush=True)
                                    else:
                                        print(f"TOOL_RESULT other: {sub}", flush=True)
                            else:
                                print(f"TOOL_RESULT raw: {c!r}", flush=True)
                else:
                    print(f"{t.upper()}: {json.dumps(f)[:300]}", flush=True)
        except websockets.ConnectionClosed:
            print("WS closed", flush=True)


if __name__ == "__main__":
    sid = sys.argv[1]
    prompt = sys.argv[2] if len(sys.argv) > 2 else "run: echo hi > /tmp/probe.txt; cat /tmp/probe.txt"
    asyncio.run(run(sid, prompt))
