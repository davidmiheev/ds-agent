"""Credential-free security regression tests: .venv/bin/python tests/test_security_hardening.py."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
with tempfile.TemporaryDirectory() as home:
    os.environ["CODING_AGENT_HOME"] = home
    from ds_agent import app, core, telegram


async def test_public_startup_auth():
    for public, password, otp in ((True, "", False), (True, "secret", False),
                                  (True, "", True), (False, "", False)):
        with patch.object(core, "APP_PUBLIC", public), patch.object(core, "APP_PASSWORD", password), \
             patch.object(telegram, "otp_enabled", return_value=otp), \
             patch.object(telegram, "is_configured", return_value=False), \
             patch.object(app.model_catalog, "openrouter_live_models", new=AsyncMock()), \
             patch.dict(os.environ, {}, clear=True):
            try:
                await app._startup_tasks()
            except RuntimeError as error:
                assert public and not password and not otp
                assert "APP_PUBLIC=1 requires" in str(error)
            else:
                assert not public or password or otp
                await asyncio.sleep(0)


async def test_workspace_containment():
    with tempfile.TemporaryDirectory() as root:
        workspace = Path(root) / "abc"
        sibling = Path(root) / "abc-other"
        workspace.mkdir()
        sibling.mkdir()
        (workspace / "ok.txt").write_text("safe")
        (sibling / "secret.txt").write_text("private")
        (workspace / "link").symlink_to(sibling, target_is_directory=True)
        with patch.object(app.sessions, "get", return_value={"workspace": str(workspace)}):
            result = await app.read_file("test", "ok.txt")
            assert Path(result.path) == workspace / "ok.txt"
            for path in ("../abc-other/secret.txt", "link/secret.txt", str(sibling / "secret.txt")):
                try:
                    await app.read_file("test", path)
                except app.HTTPException as error:
                    assert error.status_code == 403
                else:
                    raise AssertionError(f"accepted escaped path: {path}")
            try:
                await app.read_file("test", "missing.txt")
            except app.HTTPException as error:
                assert error.status_code == 404
            else:
                raise AssertionError("accepted missing file")


async def test_telegram_allowlist():
    for allowed, opt_in, expected in (([], False, False), ([], True, True),
                                       ([123], False, True), ([456], True, False)):
        with patch.object(telegram, "TELEGRAM_ALLOWED_USERS", allowed), \
             patch.object(telegram, "TELEGRAM_ALLOW_ALL_USERS", opt_in):
            assert telegram._user_allowed(123) == expected
            assert not telegram._user_allowed(None)
    api = AsyncMock()
    with patch.object(telegram, "TELEGRAM_ALLOWED_USERS", []), \
         patch.object(telegram, "TELEGRAM_ALLOW_ALL_USERS", False), \
         patch.object(telegram, "_handle_command", new=AsyncMock()) as command:
        await telegram._handle_callback_query(api, {
            "id": "cb", "data": "new:model", "from": {"id": 123},
            "message": {"chat": {"id": 123}}})
        command.assert_not_called()
        api.answer_callback_query.assert_awaited_once_with("cb", "⛔ Access denied.")
        api.call.side_effect = asyncio.CancelledError
        with patch.object(telegram, "is_configured", return_value=True), \
             patch.object(telegram, "TelegramAPI", return_value=api), \
             unittest.TestCase().assertLogs(telegram.logger, level="WARNING") as logs:
            await telegram.run_bot_polling()
        assert "all Telegram users are denied" in logs.output[0]


async def main():
    for test in (test_public_startup_auth, test_workspace_containment, test_telegram_allowlist):
        await test()
        print(f"{test.__name__}: OK")


if __name__ == "__main__":
    asyncio.run(main())
