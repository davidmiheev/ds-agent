"""Data science test v2: ask the agent to make a plot, capture everything cleanly."""
import asyncio, base64, json, re, sys, websockets
sys.stdout.reconfigure(line_buffering=True, write_through=True)


async def run(sid, prompt):
    uri = f"ws://127.0.0.1:8765/ws/sessions/{sid}"
    async with websockets.connect(uri, max_size=50_000_000) as ws:
        ready = json.loads(await ws.recv())
        print(f"READY: {ready['title']!r} / {ready['model']}", flush=True)

        await ws.send(json.dumps({"type": "user", "text": prompt}))

        tool_uses = []
        tool_results = []
        assistant_text = []
        artifacts = []
        frame_count = 0
        async for raw in ws:
            frame_count += 1
            f = json.loads(raw)
            t = f.get("type")
            # ALWAYS log every non-system frame so we can see what's flowing
            if t != "system":
                print(f"  FRAME {frame_count} t={t!r} keys={list(f.keys())}", flush=True)
                if t == "assistant":
                    content = f.get("content", [])
                    print(f"      content types: {[b.get('type') for b in content]}", flush=True)
                if t == "user":
                    content = f.get("content", [])
                    print(f"      user content types: {[b.get('type') for b in content] if isinstance(content, list) else type(content)}", flush=True)
            if t == "system":
                continue
            elif t == "assistant":
                content = f.get("content", [])
                for b in content:
                    if b.get("type") == "text" and b.get("text"):
                        assistant_text.append(b["text"])
                    elif b.get("type") == "tool_use":
                        tu = {"id": b.get("id"), "name": b.get("name"), "input": b.get("input", {})}
                        tool_uses.append(tu)
                        cmd = (b.get("input") or {}).get("command", "")[:200]
                        print(f"TOOL_USE: {b.get('name')} cmd={cmd!r}", flush=True)
                    elif b.get("type") == "thinking":
                        print(f"THINK: {b.get('thinking','')[:200]!r}...", flush=True)
            elif t == "user":
                content = f.get("content", [])
                for b in content:
                    if b.get("type") == "tool_result":
                        tr_content = b.get("content")
                        if isinstance(tr_content, str):
                            tool_results.append({"tool_use_id": b.get("tool_use_id"), "text": tr_content})
                            print(f"TOOL_RESULT (str): {tr_content[:500]!r}", flush=True)
                        elif isinstance(tr_content, list):
                            for sub in tr_content:
                                if isinstance(sub, dict) and sub.get("type") == "text":
                                    tool_results.append({"tool_use_id": b.get("tool_use_id"), "text": sub.get("text", "")})
                                    print(f"TOOL_RESULT (list/text): {sub.get('text','')[:500]!r}", flush=True)
                                else:
                                    print(f"TOOL_RESULT (list/other): {sub}", flush=True)
                                    if isinstance(sub, dict) and sub.get("data", "").startswith("data:image/"):
                                        artifacts.append({"kind": sub.get("type"), "mime": sub.get("mime_type")})
            elif t == "result":
                print(f"\nRESULT: cost=${f.get('total_cost_usd')} turns={f.get('num_turns')}", flush=True)
                print(f"result_text: {f.get('result','')!r}", flush=True)
                break
            else:
                if t != "system":
                    print(f"OTHER t={t!r}: {json.dumps(f)[:300]}", flush=True)

        print()
        print("=" * 50)
        print(f"text blocks: {len(assistant_text)}")
        for t in assistant_text:
            print(f"  > {t[:200]}")
        print(f"tool_uses: {len(tool_uses)}")
        print(f"tool_results: {len(tool_results)}")
        for tr in tool_results:
            txt = tr["text"]
            if "__ARTIFACT__" in txt:
                print(f"  >>> ARTIFACT MARKERS FOUND <<<")
                for m in re.finditer(r"__ARTIFACT__:(\w+):([^\s<]+)", txt):
                    print(f"      kind={m.group(1)} path={m.group(2)}")
            if "class=\"artifact\"" in txt:
                print(f"  >>> ARTIFACT HTML DIVS FOUND <<<")
                # extract data attrs
                for m in re.finditer(r'data-kind="(\w+)"\s+data-mime="([^"]+)"\s+data-name="([^"]+)"\s+data-b64="([^"]{0,80})', txt):
                    print(f"      kind={m.group(1)} mime={m.group(2)} name={m.group(3)} b64[:80]={m.group(4)}")
        return tool_uses, tool_results, assistant_text


if __name__ == "__main__":
    sid = sys.argv[1]
    prompt = sys.argv[2] if len(sys.argv) > 2 else (
        "Make a matplotlib plot of a sine wave and save it to /workspace/plot.png. "
        "Use the Bash tool to run a python one-liner. "
        "After saving, print the exact line: __ARTIFACT__:plot:/workspace/plot.png on its own. "
        "Then tell me in one sentence that the plot was saved."
    )
    asyncio.run(run(sid, prompt))
