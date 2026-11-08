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
        sessions._active[sid] = SimpleNamespace(client=SimpleNamespace(disconnect=disconnect))
        sessions.delete(sid)
        await asyncio.sleep(0)
        disconnect.assert_awaited_once()
        assert db.get_session(sid) is None and sid not in sessions._active
        assert all(not p.exists() for p in (workspace, legacy, sdk))
        assert db.get_session(other["id"]) is not None
        assert Path(other["workspace"]).exists()
        sessions.delete(sid)
        sessions.delete("unknown")


async def test_delete_database_failure_keeps_files():
    row = sessions.create(provider="openrouter", model="fake")
    with patch.object(db, "delete_session", side_effect=RuntimeError("database unavailable")):
        try:
            sessions.delete(row["id"])
        except RuntimeError as error:
            assert str(error) == "database unavailable"
        else:
            raise AssertionError("database error was swallowed")
    assert db.get_session(row["id"]) is not None
    assert Path(row["workspace"]).exists()


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
                 test_delete_database_failure_keeps_files, test_pdb_single_valid_request):
        await test()
        print(f"{test.__name__}: OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        _home.cleanup()
