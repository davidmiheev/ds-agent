"""Credential-free proxy/cookie regression checks."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
home = tempfile.TemporaryDirectory()
os.environ["CODING_AGENT_HOME"] = home.name
from ds_agent import app, core, db
from starlette.requests import Request


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


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        home.cleanup()
