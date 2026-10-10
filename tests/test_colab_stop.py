"""colab_stop must unassign the runtime on Colab's side, not just forget it locally.

Bug: colab_stop removed the session from the local StateStore and reported
"stopped", but never called Client.unassign(endpoint), so the VM stayed assigned
to the account (colab_sessions / list_assignments() still listed it and it kept
using quota). google-colab-cli's own `session stop` calls unassign first.

Uses a mocked client and a fake store throughout: no Google API, no real Colab
runtime, and never the real ~/.config/colab-cli/token.json (see the 2026-09-24
entry in docs/debug_notes.md) -- the token paths are redirected to a temp dir
and the test asserts the real file is byte-identical afterwards.

Run with: src/colab_mcp/.venv/bin/python tests/test_colab_stop.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from colab_mcp import colab_server as cs
from colab_cli.state import SessionState

_REAL_TOKEN_PATH = Path(cs.TOKEN_CONFIG_PATH)
_real_token_before = _REAL_TOKEN_PATH.read_bytes() if _REAL_TOKEN_PATH.exists() else None
_tmp_cfg = Path(tempfile.mkdtemp())
cs.TOKEN_CONFIG_PATH = str(_tmp_cfg / "token.json")
cs.PENDING_AUTH_PATH = str(_tmp_cfg / "pending_auth.json")


class _FakeStore:
    def __init__(self, sessions):
        self.sessions = dict(sessions)

    def list(self):
        return dict(self.sessions)

    def remove(self, name):
        self.sessions.pop(name, None)


def _session(name, endpoint):
    return SessionState(name=name, token="t", url="https://proxy.example", endpoint=endpoint)


def _stop(**args):
    result = asyncio.run(cs.call_tool("colab_stop", args))
    return json.loads(result[0].text)


def _install(sessions, active):
    client = MagicMock()
    cs._get_client = lambda: client
    cs._state.update({
        "store": _FakeStore(sessions),
        "active_session": active,
        "runtime": None,
        "client": client,
    })
    return client


_orig_get_client = cs._get_client
_orig_state = dict(cs._state)
try:
    # 1. Plain colab_stop unassigns the active session's endpoint, then forgets it.
    s = _session("sess1", "ep1")
    client = _install({"sess1": s}, s)
    out = _stop()
    client.unassign.assert_called_once_with("ep1")
    assert out == {"status": "stopped", "session": "sess1"}, out
    assert cs._state["store"].sessions == {}, "local record must be removed"
    assert cs._state["active_session"] is None
    print("colab_stop unassigns the active session's endpoint: OK")

    # 2. endpoint= for a runtime this process does not track (left behind by a
    # previous server process): unassign it, leave the active session alone.
    s = _session("sess1", "ep1")
    client = _install({"sess1": s}, s)
    out = _stop(endpoint="ep-orphan")
    client.unassign.assert_called_once_with("ep-orphan")
    assert out == {"status": "released", "endpoint": "ep-orphan"}, out
    assert set(cs._state["store"].sessions) == {"sess1"}, "unrelated session must be kept"
    assert cs._state["active_session"] is s
    print("colab_stop(endpoint=<untracked>) unassigns it and keeps the active session: OK")

    # 3. endpoint= for a runtime that still has a local store entry (not the one
    # _active_session() resolves to): the entry is dropped along with the remote
    # assignment, the other one is kept.
    stale = _session("old", "ep-old")
    current = _session("new", "ep-new")
    client = _install({"old": stale, "new": current}, None)
    out = _stop(endpoint="ep-old")
    client.unassign.assert_called_once_with("ep-old")
    assert out == {"status": "released", "endpoint": "ep-old"}, out
    assert set(cs._state["store"].sessions) == {"new"}, "only the released endpoint's entry is dropped"
    print("colab_stop(endpoint=...) drops the matching local store entry: OK")

    # 4. endpoint= equal to the active session's endpoint takes the normal stop path.
    s = _session("sess1", "ep1")
    client = _install({"sess1": s}, s)
    out = _stop(endpoint="ep1")
    client.unassign.assert_called_once_with("ep1")
    assert out == {"status": "stopped", "session": "sess1"}, out
    print("colab_stop(endpoint=<active>) behaves like a plain stop: OK")

    # 5. If unassign fails the session must NOT be reported stopped or forgotten
    # locally, otherwise the runtime is leaked with no record of it.
    s = _session("sess1", "ep1")
    client = _install({"sess1": s}, s)
    client.unassign.side_effect = RuntimeError("boom")
    result = asyncio.run(cs.call_tool("colab_stop", {}))
    assert "Error stopping" in result[0].text, result[0].text
    assert "sess1" in cs._state["store"].sessions, "local record must survive a failed unassign"
    print("failed unassign is reported and keeps the local record: OK")

    # 6. The tool schema advertises the optional endpoint argument.
    tools = asyncio.run(cs.list_tools())
    stop_tool = next(t for t in tools if t.name == "colab_stop")
    assert stop_tool.inputSchema["properties"]["endpoint"]["type"] == "string"
    assert "required" not in stop_tool.inputSchema
    print("colab_stop schema has an optional string `endpoint`: OK")
finally:
    cs._get_client = _orig_get_client
    cs._state.clear()
    cs._state.update(_orig_state)

_real_token_after = _REAL_TOKEN_PATH.read_bytes() if _REAL_TOKEN_PATH.exists() else None
assert _real_token_after == _real_token_before, "test must never touch the real Colab token file"
print("real ~/.config/colab-cli/token.json left untouched: OK")

print("\nALL COLAB_STOP CHECKS PASSED")
