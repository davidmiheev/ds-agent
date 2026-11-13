"""Credential-free proxy/cookie regression checks."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
home = tempfile.TemporaryDirectory()
os.environ["CODING_AGENT_HOME"] = home.name
from ds_agent import app, core, db, sessions
from starlette.requests import Request
from fastapi import WebSocketDisconnect


def request(peer="127.0.0.1", xff="", cookie=""):
    return Request({"type": "http", "client": (peer, 123), "headers": [
        (b"x-forwarded-for", xff.encode()), (b"cookie", cookie.encode())]})


async def main():
    for trusted, peer, xff, expected in [
        ("", "127.0.0.1", "1.2.3.4", "127.0.0.1"),
        ("127.0.0.1", "5.6.7.8", "1.2.3.4", "5.6.7.8"),
        ("127.0.0.1,10.0.0.0/8", "127.0.0.1", "1.2.3.4,10.0.0.1", "1.2.3.4"),
        ("127.0.0.1", "127.0.0.1", "spoof,5.6.7.8", "127.0.0.1"),
        ("127.0.0.1", "127.0.0.1", "1.2.3.4,5.6.7.8", "5.6.7.8"),
        ("bad-network", "127.0.0.1", "1.2.3.4", "127.0.0.1"),
        ("::1", "::1", "2001:db8::1", "2001:db8::1"),
        ("127.0.0.1", "127.0.0.1", "", "127.0.0.1"),
    ]:
        with patch.object(core, "APP_TRUSTED_PROXIES", trusted):
            assert app._client_ip(request(peer, xff)) == expected
    print("proxy trust (default, configured, spoofed, malformed, IPv6): OK")
    db.init()
    with patch.object(core, "APP_SESSION_TTL_SECONDS", 60):
        with patch.object(db.time, "time", return_value=1000):
            token = db.make_cookie_token()
            assert db.check_cookie(token)
            assert not db.check_cookie("") and not db.check_cookie("unknown")
        with patch.object(db.time, "time", return_value=1060):
            assert not db.check_cookie(token)
        fresh = db.make_cookie_token()
        response = await app.logout(request(cookie=f"sid={fresh}"))
        assert not db.check_cookie(fresh)
        with db.conn() as connection:
            assert connection.execute("SELECT 1 FROM auth_cookies WHERE token=?", (fresh,)).fetchone() is None
        assert "Max-Age=0" in response.headers["set-cookie"]
        await app.logout(request())
        with patch.object(core, "APP_PASSWORD", "test"), patch.object(app, "_otp_mode", return_value=False):
            response = await app.login_submit(request(), "test")
            assert "Max-Age=60" in response.headers["set-cookie"]
    print("cookie issuance, expiry boundary, logout revocation and browser TTL: OK")

    # An established WebSocket must lose access when its cookie is revoked —
    # re-validated per incoming command, not just at accept time.
    with patch.object(core, "APP_PASSWORD", "x"), patch.object(app, "_otp_mode", return_value=False):
        cookie = db.make_cookie_token()

        class FakeWS:
            def __init__(self, messages, revoke_at=None):
                self.cookies = {"sid": cookie}
                self._messages = list(messages)
                self._revoke_at = revoke_at
                self.sent, self.closed, self.accepted = [], None, False

            async def close(self, code=1000):
                self.closed = code

            async def accept(self):
                self.accepted = True

            async def send_text(self, data):
                self.sent.append(data)

            async def receive_text(self):
                if not self._messages:
                    raise WebSocketDisconnect()
                if self._revoke_at == 0:
                    db.delete_cookie_token(cookie)   # simulate logout mid-connection
                self._revoke_at = None if self._revoke_at is None else self._revoke_at - 1
                return self._messages.pop(0)

        async def _empty_stream(*_a, **_k):
            if False:
                yield {}

        fake_active = SimpleNamespace(db_row={}, workspace=Path("/tmp"), in_use=False)
        with patch.object(sessions, "get_or_start", AsyncMock(return_value=fake_active)), \
             patch.object(sessions, "subscribe", return_value=asyncio.Queue()), \
             patch.object(sessions, "stream_from", _empty_stream), \
             patch.object(sessions, "send_user_message", AsyncMock()) as send_um:
            ws = FakeWS([json.dumps({"type": "ping"}),
                         json.dumps({"type": "user", "text": "hi"})], revoke_at=1)
            await app.ws_session(ws, "sid-1")
            assert ws.accepted
            assert any('"pong"' in s for s in ws.sent)          # pre-revocation ping worked
            assert ws.closed == 4401                            # revoked mid-connection
            send_um.assert_not_called()                          # user command never dispatched
        print("websocket command re-validation on expiry/logout: OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        home.cleanup()
