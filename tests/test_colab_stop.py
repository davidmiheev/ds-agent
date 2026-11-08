"""Credential-free stop tests: run with src/colab_mcp/.venv/bin/python."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from colab_mcp import colab_server as cs


async def test_stop():
    for failure in (False, True):
        session = SimpleNamespace(name="test", endpoint="remote-endpoint")
        client = Mock()
        store = Mock()
        runtime = Mock()
        events = []
        def unassign(endpoint):
            events.append(("unassign", endpoint))
            if failure:
                raise RuntimeError("remote refused")
        client.unassign.side_effect = unassign
        store.remove.side_effect = lambda name: events.append(("remove", name))
        cs._state.update(active_session=session, runtime=runtime, store=store)
        with patch.object(cs, "_get_client", return_value=client):
            result = await cs.call_tool("colab_stop", {})
        client.unassign.assert_called_once_with("remote-endpoint")
        if failure:
            assert "remote refused" in result[0].text
            assert cs._state["active_session"] is session
            store.remove.assert_not_called()
        else:
            assert json.loads(result[0].text)["status"] == "stopped"
            assert cs._state["active_session"] is None
            assert events == [("unassign", "remote-endpoint"), ("remove", "test")]
        print(f"test_stop_{'failure_keeps_session' if failure else 'unassigns_before_remove'}: OK")
    cs._state["active_session"] = None
    cs._state["store"] = SimpleNamespace(list=lambda: {})
    result = await cs.call_tool("colab_stop", {})
    assert result[0].text == "No active session."
    print("test_stop_empty: OK")


if __name__ == "__main__":
    asyncio.run(test_stop())
