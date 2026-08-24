"""Quick smoke test: open a session, send a prompt, see the streamed response."""
import asyncio
import json
import sys
import websockets
# Force unbuffered stdout so we see output as it happens even with `>` redirect
sys.stdout.reconfigure(line_buffering=True, write_through=True)


async def run(sid: str, prompt: str):
    uri = f"ws://127.0.0.1:8765/ws/sessions/{sid}"
    async with websockets.connect(uri, max_size=10_000_000) as ws:
        # Greet frame
        frame = json.loads(await ws.recv())
        print(f"[ready] {frame.get('title')} / {frame.get('model')}")

        # Send the user message
        await ws.send(json.dumps({"type": "user", "text": prompt}))

        # Stream responses
        async for raw in ws:
            frame = json.loads(raw)
            t = frame.get("type")
            if t == "system":
                sub = frame.get("subtype")
                if sub == "init":
                    pass
                else:
                    print(f"[system/{sub}]", flush=True)
            elif t == "assistant":
                msg = frame.get("message", {})
                for b in msg.get("content", []):
                    if b.get("type") == "text":
                        sys.stdout.write(b.get("text", ""))
                        sys.stdout.flush()
                    elif b.get("type") == "tool_use":
                        name = b.get("name")
                        args = b.get("input", {})
                        if name == "Bash":
                            cmd = args.get("command", "")[:200]
                            print(f"\n[tool:bash] $ {cmd}", flush=True)
                        else:
                            print(f"\n[tool:{name}] {json.dumps(args)[:200]}", flush=True)
            elif t == "user":
                msg = frame.get("message", {})
                for b in msg.get("content", []):
                    if b.get("type") == "tool_result":
                        c = b.get("content")
                        if isinstance(c, list):
                            for sub in c:
                                if sub.get("type") == "text":
                                    txt = sub.get("text", "")
                                    print(f"[tool-result] {txt[:300]}", flush=True)
                                elif sub.get("type") == "image":
                                    print(f"[tool-result: image {sub.get('mime_type')}]", flush=True)
            elif t == "result":
                print(f"\n[done] cost=${frame.get('total_cost_usd')} turns={frame.get('num_turns')}", flush=True)
                return
            else:
                print(f"[{t}] {json.dumps(frame)[:200]}", flush=True)


if __name__ == "__main__":
    sid = sys.argv[1]
    prompt = sys.argv[2] if len(sys.argv) > 2 else "say only the word: HELLO"
    asyncio.run(run(sid, prompt))
