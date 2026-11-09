"""Standalone credential-free regressions: .venv/bin/python tests/test_delete_and_pdb.py."""
import asyncio
import json
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
_home = tempfile.TemporaryDirectory()
os.environ["CODING_AGENT_HOME"] = _home.name
from ds_agent import core, db, sessions
from research_mcp import server as research


async def test_delete_cleans_workspace_and_transcripts():
    db.init()
    with patch.object(Path, "home", return_value=Path(_home.name)):
        row = sessions.create(provider="openrouter", model="fake")
        sid = row["id"]
        workspace = Path(row["workspace"])
        legacy = core.DATA_DIR / "sessions" / sid
        sdk = sessions._transcript_dir(workspace)
        for directory in (workspace, legacy, sdk):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "transcript.jsonl").write_text('{"text":"private"}')
        other = sessions.create(provider="openrouter", model="fake")
        disconnect = AsyncMock()
        sessions._active[sid] = sessions.ActiveSession(sid, row, workspace, SimpleNamespace(disconnect=disconnect))
        await sessions.delete(sid)
        await asyncio.sleep(0)
        disconnect.assert_awaited_once()
        assert db.get_session(sid) is None and sid not in sessions._active
        assert all(not p.exists() for p in (workspace, legacy, sdk))
        assert db.get_session(other["id"]) is not None
        assert Path(other["workspace"]).exists()
        await sessions.delete(sid)
        await sessions.delete("unknown")


async def test_delete_database_failure_keeps_files():
    row = sessions.create(provider="openrouter", model="fake")
    with patch.object(db, "delete_session", side_effect=RuntimeError("database unavailable")):
        try:
            await sessions.delete(row["id"])
        except RuntimeError as error:
            assert str(error) == "database unavailable"
        else:
            raise AssertionError("database error was swallowed")
    assert db.get_session(row["id"]) is not None
    assert Path(row["workspace"]).exists()


async def test_delete_waits_for_disconnect_and_turn_cleanup():
    with patch.object(Path, "home", return_value=Path(_home.name)):
        row = sessions.create(provider="openrouter", model="fake")
        sid, workspace = row["id"], Path(row["workspace"])
        started, release, pump_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def disconnect():
            started.set()
            await release.wait()
            assert db.get_session(sid) is not None and workspace.exists()
            (workspace / "disconnect-final").write_text("last write")
        class Client:
            async def receive_messages(self):
                try:
                    pump_started.set()
                    await asyncio.Event().wait()
                    yield None
                finally:
                    await asyncio.sleep(0)
                    workspace.mkdir(parents=True, exist_ok=True)
                    (workspace / "turn-final").write_text("last write")
        client = Client()
        client.disconnect = disconnect
        active = sessions.ActiveSession(sid, row, workspace, client)
        sessions._active[sid] = active
        sessions.subscribe(active)
        await pump_started.wait()
        deletion = asyncio.create_task(sessions.delete(sid))
        await started.wait()
        assert not deletion.done() and db.get_session(sid) is not None and workspace.exists()
        lookup = asyncio.create_task(sessions.get_or_start(sid))
        await asyncio.sleep(0)
        assert not lookup.done()
        for action in (lambda: sessions.subscribe(active),):
            try:
                action()
            except RuntimeError as error:
                assert "closing" in str(error)
            else:
                raise AssertionError("restarted closing engine")
        release.set()
        await deletion
        assert active._engine_task.done()
        assert db.get_session(sid) is None and not workspace.exists()
        with patch.object(sessions, "_spawn", new_callable=AsyncMock) as spawn:
            try:
                await lookup
            except KeyError:
                pass
            else:
                raise AssertionError("deleted session was restarted")
            spawn.assert_not_called()
        await asyncio.sleep(0)
        assert not workspace.exists()


async def test_delete_disconnect_timeout_or_failure_preserves_files():
    for failure in ("timeout", "error"):
        row = sessions.create(provider="openrouter", model="fake")
        sid, workspace = row["id"], Path(row["workspace"])
        async def disconnect():
            if failure == "error":
                raise RuntimeError("disconnect failed")
            await asyncio.Event().wait()
        active = sessions.ActiveSession(sid, row, workspace, SimpleNamespace(disconnect=disconnect))
        sessions._active[sid] = active
        with patch.object(sessions, "_DELETE_TIMEOUT_S", 0.02):
            try:
                await sessions.delete(sid)
            except (TimeoutError, RuntimeError):
                pass
            else:
                raise AssertionError("disconnect failure was ignored")
        assert db.get_session(sid) is not None and workspace.exists()
        assert sessions._active[sid] is active
        active.client.disconnect = AsyncMock()
        await sessions.delete(sid)


async def test_delete_route_awaits_cleanup():
    from ds_agent import app
    with patch.object(sessions, "delete", new_callable=AsyncMock) as delete:
        assert await app.delete_session("test") == {"ok": True}
        delete.assert_awaited_once_with("test")


async def test_pdb_single_valid_request():
    for payload, expected in (({"result_set": [{"identifier": "1ABC"}]},
                                [{"pdb_id": "1ABC", "url": "https://www.rcsb.org/structure/1ABC"}]),
                              ({"result_set": []}, [])):
        with patch.object(research, "_http_json", return_value=payload) as request:
            assert research.pdb_search("insulin & receptor", limit=3) == expected
            request.assert_called_once()
            url = request.call_args.args[0]
            query = json.loads(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["json"][0])
            assert query["query"]["parameters"]["value"] == "insulin & receptor"
            assert query["request_options"]["paginate"]["rows"] == 3
    with patch.object(research, "_http_json", side_effect=RuntimeError("offline")) as request:
        assert research.pdb_search("insulin") == [{"error": "pdb failed: offline"}]
        request.assert_called_once()


async def main():
    for test in (test_delete_cleans_workspace_and_transcripts,
                 test_delete_database_failure_keeps_files,
                 test_delete_waits_for_disconnect_and_turn_cleanup,
                 test_delete_disconnect_timeout_or_failure_preserves_files,
                 test_delete_route_awaits_cleanup, test_pdb_single_valid_request):
        await test()
        print(f"{test.__name__}: OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        _home.cleanup()
