"""Credential-free colab_new tests: run with src/colab_mcp/.venv/bin/python."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from colab_mcp import colab_server as cs


def _assign_result(endpoint="new-endpoint"):
    info = SimpleNamespace(token="tok", url="wss://proxy", token_expires_in_seconds=3600)
    return SimpleNamespace(runtime_proxy_info=info, endpoint=endpoint)


def _reset_state(**kw):
    cs._state.update(
        creds=None, client=kw.pop("client", Mock()), store=kw.pop("store", Mock()),
        active_session=None, runtime=None, pending_auth_url=None, _auth_flow=None,
        notebook_hash=None, runtime_token_expires_at=None,
        variant=None, accelerator=None, shape=None,
    )
    cs._state.update(kw)


async def test_new_releases_previous_runtime():
    """colab_new closes the old kernel client AND unassigns the old endpoint —
    otherwise the replaced remote runtime keeps billing and is unreachable
    from every tool."""
    prev = SimpleNamespace(name="old", endpoint="old-endpoint")
    client = Mock()
    client.assign.return_value = _assign_result()
    store = Mock()
    old_runtime = Mock()
    _reset_state(client=client, store=store, active_session=prev, runtime=old_runtime)

    result = await cs.call_tool("colab_new", {})
    out = json.loads(result[0].text)

    assert out["status"] == "provisioning"
    assert out["endpoint"] == "new-endpoint"
    assert out["released_previous_endpoint"] == "old-endpoint"
    old_runtime.stop.assert_called_once()          # old kernel client closed
    client.unassign.assert_called_once_with("old-endpoint")  # remote released
    store.remove.assert_called_once_with("old")    # dropped from session store
    store.add.assert_called_once()                 # new session stored
    assert cs._state["active_session"].endpoint == "new-endpoint"
    assert cs._state["runtime"] is None            # lazy init, not a leaked ref
    print("test_new_releases_previous_runtime: OK")


async def test_new_with_no_previous():
    """First-ever colab_new assigns without touching unassign."""
    client = Mock()
    client.assign.return_value = _assign_result()
    store = Mock()
    store.list.return_value = {}
    _reset_state(client=client, store=store)

    result = await cs.call_tool("colab_new", {})
    out = json.loads(result[0].text)

    assert out["status"] == "provisioning"
    assert "released_previous_endpoint" not in out
    client.unassign.assert_not_called()
    print("test_new_with_no_previous: OK")


async def test_new_does_not_release_foreign_session():
    """A session that exists only in the shared StateStore (created by another
    process or the CLI) must NOT be unassigned — it isn't ours to kill."""
    foreign = SimpleNamespace(name="foreign", endpoint="foreign-endpoint")
    client = Mock()
    client.assign.return_value = _assign_result()
    store = Mock()
    store.list.return_value = {"foreign": foreign}
    _reset_state(client=client, store=store)   # active_session=None, runtime=None

    result = await cs.call_tool("colab_new", {})
    out = json.loads(result[0].text)

    assert out["status"] == "provisioning"
    assert "released_previous_endpoint" not in out
    client.unassign.assert_not_called()
    store.remove.assert_not_called()
    print("test_new_does_not_release_foreign_session: OK")


async def test_new_survives_unassign_failure():
    """A failed release of the previous runtime must not block provisioning."""
    prev = SimpleNamespace(name="old", endpoint="old-endpoint")
    client = Mock()
    client.assign.return_value = _assign_result()
    client.unassign.side_effect = RuntimeError("remote refused")
    store = Mock()
    _reset_state(client=client, store=store, active_session=prev, runtime=Mock())

    result = await cs.call_tool("colab_new", {})
    out = json.loads(result[0].text)

    assert out["status"] == "provisioning"
    assert "released_previous_endpoint" not in out
    store.remove.assert_not_called()               # prev session kept for retry
    assert cs._state["active_session"].endpoint == "new-endpoint"
    print("test_new_survives_unassign_failure: OK")


async def test_new_failed_assign_keeps_previous():
    """If assign() raises, the previous session/runtime must stay untouched."""
    prev = SimpleNamespace(name="old", endpoint="old-endpoint")
    client = Mock()
    client.assign.side_effect = RuntimeError("quota")
    store = Mock()
    old_runtime = Mock()
    _reset_state(client=client, store=store, active_session=prev, runtime=old_runtime)

    result = await cs.call_tool("colab_new", {})

    assert result[0].text.startswith("Error: quota")
    assert cs._state["active_session"] is prev
    assert cs._state["runtime"] is old_runtime     # NOT dropped
    client.unassign.assert_not_called()
    print("test_new_failed_assign_keeps_previous: OK")


if __name__ == "__main__":
    asyncio.run(test_new_releases_previous_runtime())
    asyncio.run(test_new_with_no_previous())
    asyncio.run(test_new_does_not_release_foreign_session())
    asyncio.run(test_new_survives_unassign_failure())
    asyncio.run(test_new_failed_assign_keeps_previous())
