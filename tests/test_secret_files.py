"""Run with .venv/bin/python tests/test_secret_files.py; no credentials needed."""
import asyncio
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
home = tempfile.TemporaryDirectory()
os.environ["CODING_AGENT_HOME"] = home.name
from ds_agent import app, core, sessions, artifact_parser, export, crypto
from ds_agent.workspace_files import artifact_path


async def main():
    root = Path(home.name)
    ws = root / "workspace"
    ws.mkdir()
    row = {"workspace": str(ws), "mcp_overrides": "{}"}
    sessions._render_session_dir(row, {"TOKEN": "test-secret"}, ws)
    for p in (ws, ws / ".claude"):
        assert stat.S_IMODE(p.stat().st_mode) == 0o700
    for p in (ws / ".mcp.json", ws / ".claude/settings.local.json"):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        p.chmod(0o644)
    sessions._render_session_dir(row, {}, ws)
    assert (ws / ".mcp.json").stat().st_mode & 0o777 == 0o600
    await app.save_mcp({"raw": '{"mcpServers": {}}'})
    crypto.save_key("test", "fake-key")
    for p in (core.MCP_CONFIG_PATH, core.KEYS_PATH, core.VAULT_KEY_PATH):
        assert p.stat().st_mode & 0o777 == 0o600
    print("private configuration permissions: OK")
    safe = ws / "safe.txt"
    safe.write_text("public-data")
    outside = root / "outside.txt"
    outside.write_text("private-outside")
    (ws / "escape").symlink_to(outside)
    (ws / "alias").symlink_to(ws / ".mcp.json")
    rejected = [".mcp.json", ".claude/settings.local.json", "alias", "escape", str(outside), "../outside.txt"]
    with patch.object(app.sessions, "get", return_value=row):
        files = await app.list_files("test")
        assert [f["path"] for f in files] == ["safe.txt"]
        assert Path((await app.read_file("test", "safe.txt")).path) == safe
        for path in rejected:
            try:
                await app.read_file("test", path)
            except app.HTTPException as error:
                assert error.status_code == 403
            else:
                raise AssertionError(path)
    print("file routes exclude secrets and symlink escapes: OK")
    for path in rejected + ["missing.txt"]:
        assert artifact_path(path, ws) is None
        assert export._resolve_artifact_path(path, ws) is None
        assert 'data-b64=' not in artifact_parser.rewrite_tool_result_content(f"__ARTIFACT__:text:{path}", ws)
    assert artifact_path("safe.txt", ws) == safe
    assert 'data-b64=' in artifact_parser.rewrite_tool_result_content(f"__ARTIFACT__:text:{safe}", ws)
    assert artifact_path(str(safe), None) is None
    print("artifact and export boundaries (normal, missing, private, escaped): OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        home.cleanup()
