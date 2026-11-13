"""Credential-free runtime lifecycle tests. Run with the Colab venv's Python."""
import asyncio
import json
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from colab_mcp import colab_server as cs


class FakeRuntime:
    instances = []
    fail_warmup = False
    failure = None
    started = threading.Event()
    release = threading.Event()

    def __init__(self, url, token, **kwargs):
        self.ids = kwargs
        self.closed = False
        self.calls = []
        self.instances.append(self)

    def execute_code(self, code, timeout=None):
        self.calls.append((code, timeout))
        if "os.chdir" in code:
            assert timeout == cs._WARMUP_TIMEOUT_S
            if self.fail_warmup:
                raise TimeoutError("warmup timed out")
        elif code == "slow":
            self.started.set()
            assert self.release.wait(3), "test did not release execution"
        elif self.failure:
            raise self.failure
        elif code == "user_error":
            return [{"output_type": "error", "ename": "ValueError",
                     "evalue": "bad input", "traceback": []}]
        return []

    def stop(self):
        self.closed = True


def reset():
    FakeRuntime.instances = []
    FakeRuntime.fail_warmup = False
    FakeRuntime.failure = None
    FakeRuntime.started = threading.Event()
    FakeRuntime.release = threading.Event()
    cs._state.update(active_session=SimpleNamespace(
        name="test", url="https://fake", token="fake", endpoint="endpoint",
        kernel_id="same-kernel", session_id="same-session", accelerator="NONE",
        variant="DEFAULT", machine_shape="STANDARD"), runtime=None,
        runtime_token_expires_at=time.time() + 3600, notebook_hash=uuid.UUID(int=1),
        store=SimpleNamespace(add=lambda s: None, list=lambda: {}))


async def test_status_and_sessions_during_execute():
    reset()
    execute = asyncio.create_task(cs.call_tool("colab_execute", {"code": "slow"}))
    try:
        assert await asyncio.to_thread(FakeRuntime.started.wait, 2)
        result = await asyncio.wait_for(cs.call_tool("colab_status", {}), .5)
        assert json.loads(result[0].text)["kernel_id"] == "same-kernel"
        with patch.object(cs, "_get_creds", return_value=None), patch.object(cs, "Client") as client:
            client.return_value.list_assignments.return_value = []
            result = await asyncio.wait_for(cs.call_tool("colab_sessions", {}), .5)
            assert json.loads(result[0].text) == {"sessions": []}
        assert not execute.done()
    finally:
        FakeRuntime.release.set()
        await execute


async def test_failed_warmup_retries():
    reset()
    FakeRuntime.fail_warmup = True
    result = await cs.call_tool("colab_execute", {"code": "pass"})
    assert "warmup timed out" in result[0].text
    assert cs._state["runtime"] is None and FakeRuntime.instances[0].closed
    FakeRuntime.fail_warmup = False
    await cs.call_tool("colab_execute", {"code": "pass"})
    assert len(FakeRuntime.instances) == 2
    assert cs._state["runtime"] is FakeRuntime.instances[1]


async def test_transport_failure_reconnects_without_replay():
    reset()
    FakeRuntime.failure = RuntimeError("Connection was lost.")
    await cs.call_tool("colab_execute", {"code": "side_effect()"})
    old = FakeRuntime.instances[0]
    assert old.closed and cs._state["runtime"] is None
    assert sum(code == "side_effect()" for code, _ in old.calls) == 1
    FakeRuntime.failure = None
    await cs.call_tool("colab_execute", {"code": "pass"})
    new = cs._state["runtime"]
    assert new is not old
    assert new.ids["kernel_id"] == "same-kernel"
    assert new.ids["session_id"] == "same-session"


async def test_user_error_keeps_runtime():
    reset()
    result = await cs.call_tool("colab_execute", {"code": "user_error"})
    assert "ValueError" in result[0].text
    runtime = cs._state["runtime"]
    assert not runtime.closed
    await cs.call_tool("colab_execute", {"code": "pass"})
    assert cs._state["runtime"] is runtime


async def test_refresh_closes_old_runtime():
    reset()
    await cs.call_tool("colab_execute", {"code": "pass"})
    old = cs._state["runtime"]
    cs._state["runtime_token_expires_at"] = 0
    response = SimpleNamespace(endpoint="endpoint", runtime_proxy_info=SimpleNamespace(
        token="fresh", url="https://fake", token_expires_in_seconds=3600))
    with patch.object(cs, "_get_client", return_value=SimpleNamespace(assign=lambda *a, **k: response)):
        await cs.call_tool("colab_execute", {"code": "pass"})
    assert old.closed and cs._state["runtime"] is not old


async def test_upload_transport_failure():
    reset()
    await cs.call_tool("colab_execute", {"code": "pass"})
    old = cs._state["runtime"]
    with tempfile.NamedTemporaryFile(suffix=".csv") as local:
        with patch("colab_cli.contents.ContentsClient") as contents:
            contents.return_value.upload.side_effect = cs.requests.ConnectionError("disconnected")
            result = await cs.call_tool("colab_upload", {"local_path": local.name})
            assert "disconnected" in result[0].text
            contents.return_value.upload.assert_called_once()
    assert old.closed and cs._state["runtime"] is None


async def test_dead_and_restarting_kernels():
    for state in ("dead", "restarting"):
        reset()
        saved = []
        cs._state["store"].add = lambda s: saved.append((s.kernel_id, s.session_id))
        FakeRuntime.failure = RuntimeError(f"Kernel {state} before the code finished.")
        result = await cs.call_tool("colab_execute", {"code": "side_effect()"})
        old = FakeRuntime.instances[0]
        assert old.closed and cs._state["runtime"] is None
        assert sum(code == "side_effect()" for code, _ in old.calls) == 1
        FakeRuntime.failure = None
        await cs.call_tool("colab_execute", {"code": "next_call"})
        ids = FakeRuntime.instances[-1].ids
        if state == "dead":
            assert ids["kernel_id"] is None and ids["session_id"] is None
            assert saved == [(None, None)]
            assert "state was lost" in result[0].text and "fresh kernel" in result[0].text
        else:
            assert ids["kernel_id"] == "same-kernel" and ids["session_id"] == "same-session"
            assert not saved and "state was lost" not in result[0].text


async def test_proxy_auth_refresh_only_for_proxy_urls():
    for status in (401, 403):
        for url, refresh in (("https://fake/proxy/api/kernels", True),
                             ("https://fake/proxy", True),
                             ("https://fake/proxy-other", False),
                             ("https://fake/control-plane", False),
                             ("https://oauth.example/token", False), (None, False)):
            reset()
            s = cs._state["active_session"]
            s.url = "https://fake/proxy/"
            notebook_hash = uuid.UUID(int=42)
            cs._state["notebook_hash"] = notebook_hash
            deadline = cs._state["runtime_token_expires_at"]
            response = cs.requests.Response()
            response.status_code, response.url = status, url
            FakeRuntime.failure = cs.requests.HTTPError("rejected", response=response)
            await cs.call_tool("colab_execute", {"code": "side_effect()"})
            assert cs._state["runtime"] is None
            assert cs._state["runtime_token_expires_at"] == (None if refresh else deadline)
            assigned = []
            def assign(hash_value, **kwargs):
                assigned.append(hash_value)
                return SimpleNamespace(runtime_proxy_info=SimpleNamespace(
                    token="fresh", url=s.url, token_expires_in_seconds=3600))
            FakeRuntime.failure = None
            with patch.object(cs, "_get_client", return_value=SimpleNamespace(assign=assign)):
                await cs.call_tool("colab_execute", {"code": "next_call"})
            assert assigned == ([notebook_hash] if refresh else [])
            assert FakeRuntime.instances[-1].ids["kernel_id"] == "same-kernel"


async def test_runtime_operations_are_serialized():
    reset()
    execute = asyncio.create_task(cs.call_tool("colab_execute", {"code": "slow"}))
    try:
        assert await asyncio.to_thread(FakeRuntime.started.wait, 2)
        second = asyncio.create_task(cs.call_tool("colab_execute", {"code": "second"}))
        await asyncio.sleep(.05)
        assert not second.done()
        assert all(code != "second" for code, _ in FakeRuntime.instances[0].calls)
    finally:
        FakeRuntime.release.set()
        await execute
    await second
    assert FakeRuntime.instances[0].calls[-1][0] == "second"


async def main():
    with patch.object(cs, "ColabRuntime", FakeRuntime):
        for test in (test_status_and_sessions_during_execute, test_failed_warmup_retries,
                     test_transport_failure_reconnects_without_replay,
                     test_user_error_keeps_runtime, test_refresh_closes_old_runtime,
                     test_upload_transport_failure, test_runtime_operations_are_serialized,
                     test_dead_and_restarting_kernels, test_proxy_auth_refresh_only_for_proxy_urls):
            await test()
            print(f"{test.__name__}: OK")


if __name__ == "__main__":
    asyncio.run(main())
