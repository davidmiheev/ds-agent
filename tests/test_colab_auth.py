"""End-to-end: ask the agent to call colab_auth (no code), get an auth URL back."""
import asyncio
import json
import urllib.request
import http.cookiejar
import websockets


async def main():
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.open("http://127.0.0.1:8765/")

    req = urllib.request.Request(
        "http://127.0.0.1:8765/v1/sessions",
        data=json.dumps({"provider": "openrouter", "model": "anthropic/claude-sonnet-4-5"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = opener.open(req)
    data = json.loads(resp.read().decode())
    sid = data["id"]
    print(f"created session: {sid}")

    cookies = {c.name: c.value for c in jar}
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    extra = [("Cookie", cookie_str)]

    uri = f"ws://127.0.0.1:8765/ws/sessions/{sid}"
    async with websockets.connect(uri, additional_headers=extra) as ws:
        await ws.recv()  # greet

        await ws.send(json.dumps({
            "type": "user",
            "text": (
                "First call the `WaitForMcpServers` tool. Then call "
                "`mcp__colab__colab_auth` (no arguments) and report the "
                "auth_url field from its result verbatim. Don't do anything "
                "else — no installs, no setup."
            ),
        }))

        deadline = asyncio.get_event_loop().time() + 90
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=85)
            except asyncio.TimeoutError:
                print("TIMEOUT")
                break
            msg = json.loads(raw)
            t = msg.get("type", "?")
            if t == "assistant":
                for c in msg.get("content", []):
                    if c.get("type") == "text" and c.get("text", "").strip():
                        print(f"  TEXT: {c['text'][:200]}")
                    elif c.get("type") == "tool_use":
                        print(f"  TOOL_USE: {c.get('name')}  args={json.dumps(c.get('input', {}))[:200]}")
            elif t == "user":
                for c in msg.get("content", []):
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        out = c.get("content", "")
                        if isinstance(out, list):
                            out = " | ".join(str(x)[:300] for x in out)
                        print(f"  TOOL_RESULT: {str(out)[:600]}")
            elif t == "result":
                print(f"  RESULT: {str(msg)[:200]}")
                return


asyncio.run(main())
