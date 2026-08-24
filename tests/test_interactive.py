"""Verbose test: print everything, also print result.result field."""
import asyncio, json, sys, websockets
sys.stdout.reconfigure(line_buffering=True, write_through=True)

async def run(sid, prompt):
    uri = f"ws://127.0.0.1:8765/ws/sessions/{sid}"
    async with websockets.connect(uri, max_size=50_000_000) as ws:
        ready = json.loads(await ws.recv())
        print(f"READY: {ready}", flush=True)

        await ws.send(json.dumps({"type": "user", "text": prompt}))

        try:
            async for raw in ws:
                f = json.loads(raw)
                t = f.get("type")
                if t == "system":
                    print(f"SYS/{f.get('subtype')}: {json.dumps(f)[:200]}", flush=True)
                elif t == "assistant":
                    print(f"ASSIST FULL: {json.dumps(f)[:600]}", flush=True)
                    msg = f.get("message", {})
                    print(f"  message keys: {list(msg.keys())}", flush=True)
                elif t == "user":
                    msg = f.get("message", {})
                    print(f"USER: keys={list(msg.keys())}", flush=True)
                    for b in msg.get("content", []):
                        if b.get("type") == "tool_result":
                            c = b.get("content")
                            if isinstance(c, list):
                                for sub in c:
                                    if sub.get("type") == "text":
                                        print(f"  TOOL_RESULT: {sub.get('text','')[:400]!r}", flush=True)
                                    else:
                                        print(f"  TOOL_RESULT sub: {sub}", flush=True)
                            else:
                                print(f"  TOOL_RESULT raw: {c!r}", flush=True)
                elif t == "tool_use":
                    print(f"TOOL_USE top-level: name={f.get('name')} input={json.dumps(f.get('input', {}))[:300]}", flush=True)
                elif t == "tool_result":
                    print(f"TOOL_RESULT top-level: {json.dumps(f)[:400]}", flush=True)
                elif t == "result":
                    print(f"RESULT: turns={f.get('num_turns')} cost=${f.get('total_cost_usd')}", flush=True)
                    print(f"  result_text: {f.get('result','')!r}", flush=True)
                    print(f"  stop_reason: {f.get('stop_reason')!r}", flush=True)
                    break
                else:
                    print(f"OTHER type={t!r}: {json.dumps(f)[:400]}", flush=True)
        except websockets.ConnectionClosed:
            print("WS closed", flush=True)


if __name__ == "__main__":
    sid = sys.argv[1]
    prompt = sys.argv[2] if len(sys.argv) > 2 else "what is 2+2? reply in one word."
    asyncio.run(run(sid, prompt))
