"""End-to-end: ask the agent to call colab_status. Should trigger the auth
flow because there's no token yet."""
import asyncio
import json
import re
import urllib.request
import http.cookiejar
import websockets


async def main():
    # Get auth cookie
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.open("http://127.0.0.1:8765/")

    # Create a session via the REST API
    import os
    req = urllib.request.Request(
        "http://127.0.0.1:8765/v1/sessions",
        data=json.dumps({"provider": "openrouter", "model": "anthropic/claude-sonnet-4.5"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = opener.open(req)
    data = json.loads(resp.read().decode())
    sid = data["id"]
    print(f"created session: {sid}  ({data.get('title')})")

    # Get cookies
    cookies = {c.name: c.value for c in jar}
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

    uri = f"ws://127.0.0.1:8765/ws/sessions/{sid}"
    extra = [("Cookie", cookie_str)]
    async with websockets.connect(uri, additional_headers=extra) as ws:
        # wait for greet
        greet = json.loads(await ws.recv())
        print(f"greet: {greet.get('type')}")

        await ws.send(json.dumps({
            "type": "user",
            "text": (
                "First call the `WaitForMcpServers` tool to wait for all MCP "
                "servers to be ready. Then call `mcp__colab__colab_status` "
                "and report what it returns. Don't do anything else."
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
                        print(f"  TOOL_USE: {c.get('name')}  args={json.dumps(c.get('input', {}))[:150]}")
            elif t == "user":
                for c in msg.get("content", []):
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        out = c.get("content", "")
                        if isinstance(out, list):
                            out = " | ".join(str(x)[:200] for x in out)
                        print(f"  TOOL_RESULT: {str(out)[:500]}")
            elif t == "result":
                print(f"  RESULT: {str(msg)[:200]}")
                return
            elif t == "system":
                print(f"  [system] {str(msg)[:200]}")
            else:
                print(f"  [{t}] {str(msg)[:200]}")


asyncio.run(main())
