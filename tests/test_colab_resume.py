"""Assignment identity survives process-state loss; no credentials needed."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from colab_mcp import colab_server as cs


class Runtime:
    instances = []

    def __init__(self, url, token, **kwargs):
        self.ids = kwargs
        self.calls = []
        self.instances.append(self)
        if not kwargs["kernel_id"]:
            kwargs["on_kernel_started"]("original-kernel")
            kwargs["on_session_started"]("original-session")

    def execute_code(self, code, timeout=None):
        self.calls.append(code)
        return []

    def stop(self):
        pass


async def main():
    original = dict(cs._state)
    try:
        with tempfile.TemporaryDirectory() as directory:
            store_path = str(Path(directory) / "sessions.json")
            cs._state.update({k: None for k in cs._state})
            cs._state["store"] = cs.StateStore(store_path)
            assigned = []
            def assign(notebook_hash, **options):
                assigned.append((notebook_hash, options))
                return SimpleNamespace(endpoint="original-endpoint", runtime_proxy_info=SimpleNamespace(
                    token="fake", url="https://fake", token_expires_in_seconds=3600))
            with patch.object(cs, "_get_client", return_value=SimpleNamespace(assign=assign)), patch.object(cs, "ColabRuntime", Runtime):
                result = await cs.call_tool("colab_new", {"session": "test", "gpu": "T4", "high_mem": True})
                assert json.loads(result[0].text)["status"] == "provisioning"
                await cs.call_tool("colab_execute", {"code": "first"})
                saved = cs._state["store"].get("test")
                metadata_path = cs._resume_path(saved)
                assert metadata_path.stat().st_mode & 0o777 == 0o600
                # A fresh store instance also discards any in-memory persistence caches.
                cs._state.update({k: None for k in cs._state})
                cs._state["store"] = cs.StateStore(store_path)
                with patch.object(cs.uuid, "uuid4", side_effect=AssertionError("new hash after restart")):
                    result = await cs.call_tool("colab_execute", {"code": "after_restart"})
                assert "error" not in result[0].text.lower(), result
                assert len(assigned) == 2 and assigned[0] == assigned[1]
                assert assigned[1][1] == {"variant": cs.Variant.GPU, "accelerator": cs.Accelerator.T4, "shape": cs.Shape.HIGH_RAM}
                assert Runtime.instances[-1].ids["kernel_id"] == "original-kernel"
                assert Runtime.instances[-1].ids["session_id"] == "original-session"
                assert "after_restart" in Runtime.instances[-1].calls
                print("restart refresh uses saved hash/options and reconnects to same kernel/session: OK")
                for content in (None, "{", json.dumps({"endpoint": "wrong"})):
                    if content is None:
                        metadata_path.unlink()
                    else:
                        metadata_path.write_text(content)
                    cs._state.update(active_session=None, runtime=None, notebook_hash=None)
                    result = await cs.call_tool("colab_execute", {"code": "must_not_run"})
                    assert "Cannot resume" in result[0].text
                    assert len(assigned) == 2
                cs._state["store"].remove("test")
                result = await cs.call_tool("colab_execute", {"code": "must_not_run"})
                assert "No active session" in result[0].text
                print("missing/corrupt/mismatched identity and empty store never provision: OK")

            # Default RAM: resolve_assign_shape returns None — _save_resume must
            # persist null, not crash after assign() already billed a runtime.
            cs._state.update({k: None for k in cs._state})
            cs._state["store"] = cs.StateStore(store_path)
            with patch.object(cs, "_get_client", return_value=SimpleNamespace(assign=assign)):
                result = await cs.call_tool("colab_new", {"session": "default-ram"})
                out = json.loads(result[0].text)
                assert out["status"] == "provisioning", out
                data = json.loads(cs._resume_path(cs._state["store"].get("default-ram")).read_text())
                assert data["shape"] is None, data
            print("default RAM persists shape=null without crashing: OK")

            # Refresh returning a DIFFERENT endpoint = the saved runtime is gone;
            # the replacement must be released and the call must fail, not target
            # the old kernel through the new proxy.
            cs._state.update({k: None for k in cs._state})
            cs._state["store"] = cs.StateStore(store_path)
            released = []
            def assign_new(notebook_hash, **options):
                return SimpleNamespace(endpoint="new-endpoint", runtime_proxy_info=SimpleNamespace(
                    token="fake", url="https://fake", token_expires_in_seconds=3600))
            client = SimpleNamespace(assign=assign_new,
                                     unassign=lambda endpoint: released.append(endpoint))
            with patch.object(cs, "_get_client", return_value=client), \
                 patch.object(cs, "ColabRuntime", Runtime):
                result = await cs.call_tool("colab_execute", {"code": "must_not_run"})
                assert "gone" in result[0].text, result
                assert released == ["new-endpoint"], released
            print("refresh releases a replacement runtime instead of reusing it: OK")
    finally:
        cs._state.clear()
        cs._state.update(original)


if __name__ == "__main__":
    asyncio.run(main())
