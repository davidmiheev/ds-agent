"""Data science smoke test: ask the agent to make a matplotlib plot and watch for
the __ARTIFACT__:plot marker. The server-side artifact parser should turn that
into a <div class="artifact" data-b64="..."> in the streamed text.
"""
import asyncio
import base64
import json
import re
import sys
import websockets
sys.stdout.reconfigure(line_buffering=True, write_through=True)


async def run(sid: str, prompt: str):
    uri = f"ws://127.0.0.1:8765/ws/sessions/{sid}"
    async with websockets.connect(uri, max_size=50_000_000) as ws:
        frame = json.loads(await ws.recv())
        print(f"[ready] {frame.get('title')!r} / {frame.get('model')}", flush=True)

        await ws.send(json.dumps({"type": "user", "text": prompt}))

        saw_text = []
        saw_tool = False
        saw_artifact = False
        artifact_paths = []
        async for raw in ws:
            frame = json.loads(raw)
            t = frame.get("type")
            if t == "system":
                continue
            elif t == "assistant":
                msg = frame.get("message", {})
                for b in frame.get("content") or msg.get("content") or []:
                    if b.get("type") == "text" and b.get("text"):
                        saw_text.append(b["text"])
                        sys.stdout.write(b["text"])
                        sys.stdout.flush()
                    elif b.get("type") == "tool_use":
                        saw_tool = True
                        name = b.get("name")
                        args = b.get("input", {})
                        if name == "Bash":
                            cmd = args.get("command", "")[:300]
                            print(f"\n[tool:bash] $ {cmd}", flush=True)
                        else:
                            print(f"\n[tool:{name}] {json.dumps(args)[:200]}", flush=True)
            elif t == "user":
                msg = frame.get("message", {})
                for b in frame.get("content") or msg.get("content") or []:
                    if b.get("type") == "tool_result":
                        c = b.get("content")
                        if isinstance(c, list):
                            for sub in c:
                                if sub.get("type") == "text":
                                    txt = sub.get("text", "")
                                    if "__ARTIFACT__" in txt:
                                        saw_artifact = True
                                        artifact_paths.extend(re.findall(r"__ARTIFACT__:(\w+):([^\s]+)", txt))
                                    print(f"[tool-result] {txt[:500]}", flush=True)
            elif t == "result":
                print(f"\n\n[done] cost=${frame.get('total_cost_usd')} turns={frame.get('num_turns')}", flush=True)
                break

        print()
        print("=" * 50)
        print(f"saw assistant text: {bool(saw_text)} ({len(saw_text)} blocks)")
        print(f"saw tool use:       {saw_tool}")
        print(f"saw __ARTIFACT__ markers in tool output: {saw_artifact}")
        print(f"artifacts: {artifact_paths}")
        return saw_text, saw_tool, saw_artifact, artifact_paths


if __name__ == "__main__":
    sid = sys.argv[1]
    prompt = sys.argv[2] if len(sys.argv) > 2 else (
        "Use the Bash tool to run a python one-liner that: "
        "(1) creates /workspace/plot.png with a matplotlib sine wave, "
        "(2) prints the line '__ARTIFACT__:plot:/workspace/plot.png' on its own. "
        "Then confirm in one short sentence that the file is there."
    )
    asyncio.run(run(sid, prompt))
