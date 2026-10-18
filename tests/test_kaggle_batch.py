"""Unit tests for ds_agent/kaggle_batch.py: no network, Kaggle is a fake.

Run with: PYTHONPATH=src python tests/test_kaggle_batch.py   (or pytest)
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ds_agent import kaggle_batch as kb  # noqa: E402


class FakeKaggle:
    """Notebooks finish when told to; outputs are given per slug."""

    def __init__(self):
        self.pushed: list[str] = []
        self.state: dict[str, str] = {}
        self.outputs: dict[str, dict[str, str]] = {}   # slug -> {file name: content}
        self.fail_push = False

    async def call_tool(self, name, arguments):
        req = arguments.get("request", {})
        if name == "save_notebook":
            assert req["kernelTypeNullable"] == "script" and req["languageNullable"] == "python"
            if self.fail_push:
                return {"content": [{"type": "text", "text": "quota"}], "isError": True}
            slug = req["slugNullable"].split("/")[1]
            self.pushed.append(slug)
            self.state[slug] = "RUNNING"
            return {"content": [{"type": "text", "text": json.dumps({"ref": slug})}]}
        if name == "get_notebook_session_status":
            return {"content": [{"type": "text", "text": json.dumps({"status": self.state[req["kernelSlug"]]})}]}
        if name == "list_notebook_session_output":
            files = [{"file_name": f, "url": f"https://x/kf/42/{req['kernelSlug']}/{f}"}
                     for f in self.outputs.get(req["kernelSlug"], {})]
            return {"content": [{"type": "text", "text": json.dumps({"files": files})}]}
        if name == "cancel_notebook_session":
            return {"content": [{"type": "text", "text": json.dumps({"ok": req["kernelSessionId"]})}]}
        raise AssertionError(name)

    def download(self, url, dest: Path):
        slug, fname = url.split("/")[-2:]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(self.outputs[slug][fname])


def _run(coro):
    return asyncio.run(coro)


def _fresh_dir():
    d = tempfile.mkdtemp()
    os.environ["KAGGLE_BATCH_DIR"] = d
    return d


OPTS = dict(done_regex=r"### SHARD DONE", fail_regex=r"FAILED", deadline_regex=r"### deadline reached")


def test_cap_across_batches_and_collect():
    _fresh_dir()
    fk = FakeKaggle()
    kb.create_batch("a", [{"slug": f"job-a{i}", "script": "print(1)"} for i in range(4)], owner="me",
                    max_running=5, **OPTS)
    kb.create_batch("b", [{"slug": f"job-b{i}", "script": "print(1)"} for i in range(3)], owner="me",
                    max_running=5, **OPTS)
    _run(kb.step("a", fk.call_tool, download=fk.download))
    s = _run(kb.step("b", fk.call_tool, download=fk.download))
    assert len(fk.pushed) == 5, fk.pushed                     # 4 from a, 1 from b: the cap holds across batches
    assert s["counts"] == {"running": 1, "queued": 2}
    fk.state["job-a0"] = "COMPLETE"
    fk.outputs["job-a0"] = {"c10_0_2.log": "1/2 X ok\n### SHARD DONE test 0/2\n",
                            "c10_1_2.log": "1/2 Y FAILED boom\n### SHARD DONE test 1/2\n"}
    fk.state["job-a1"] = "COMPLETE"
    fk.outputs["job-a1"] = {"c10_0_2.log": "### SHARD DONE test 0/2\n"}
    s = _run(kb.step("a", fk.call_tool, download=fk.download))
    jobs = {j["slug"]: j for j in s["jobs"]}
    assert jobs["job-a0"]["status"] == "collected" and jobs["job-a0"]["outcome"] == "failures"
    assert jobs["job-a1"]["outcome"] == "ok"
    # Two slots freed: b's queued jobs go out on its next step.
    s = _run(kb.step("b", fk.call_tool, download=fk.download))
    assert s["counts"] == {"running": 3}, s["counts"]


def test_deadline_and_missing_done_are_failures():
    d = Path(_fresh_dir())
    (d / "o").mkdir()
    (d / "o" / "a.log").write_text("1/9 X ok\n### deadline reached\n### SHARD DONE p 0/1\n")
    (d / "o" / "b.log").write_text("1/9 X ok\n")
    logs, outcome = kb.scan_logs(d / "o", {**kb.DEFAULTS, **OPTS})
    assert outcome == "failures"
    assert logs["a.log"]["deadline"] and logs["b.log"]["done"] is False


def test_retry_and_push_failure():
    _fresh_dir()
    fk = FakeKaggle()
    kb.create_batch("r", [{"slug": "job-r0", "script": "print(1)"}], owner="me", **OPTS)
    fk.fail_push = True
    s = _run(kb.step("r", fk.call_tool, download=fk.download))
    assert s["counts"] == {"queued": 1} and "push failed" in s["events"][-1]
    fk.fail_push = False
    _run(kb.step("r", fk.call_tool, download=fk.download))
    fk.state["job-r0"] = "ERROR"
    fk.outputs["job-r0"] = {}
    s = _run(kb.step("r", fk.call_tool, download=fk.download))
    assert s["jobs"][0]["outcome"] == "error"
    try:
        kb.retry("r", "nope")
        raise AssertionError("unknown slug accepted")
    except kb.BatchError:
        pass
    s = kb.retry("r", "job-r0")
    assert s["counts"] == {"queued": 1}
    _run(kb.step("r", fk.call_tool, download=fk.download))
    assert fk.pushed == ["job-r0", "job-r0"]


def test_slug_clash_across_batches_and_cancel():
    _fresh_dir()
    fk = FakeKaggle()
    kb.create_batch("x", [{"slug": "job-x0", "script": "print(1)"}], owner="me")
    try:
        kb.create_batch("y", [{"slug": "job-x0", "script": "print(1)"}], owner="me")
        raise AssertionError("clash accepted")
    except kb.BatchError:
        pass
    assert "dropped" in _run(kb.cancel("x", "job-x0", fk.call_tool))["result"]
    kb.create_batch("z", [{"slug": "job-z0", "script": "print(1)"}], owner="me")
    _run(kb.step("z", fk.call_tool, download=fk.download))
    fk.outputs["job-z0"] = {"a.log": "partial"}
    res = _run(kb.cancel("z", "job-z0", fk.call_tool))
    assert res["kernel_session_id"] == 42


def test_tool_dispatch():
    _fresh_dir()
    fk = FakeKaggle()
    out = _run(kb.dispatch("batch_create", {"name": "t", "owner": "me", "max_running": 2,
                                            "jobs": [{"slug": "job-t0", "script": "print(1)"}]}, fk.call_tool))
    assert out["counts"] == {"queued": 1}
    assert {t["name"] for t in kb.TOOLS} == kb.TOOL_NAMES
    assert _run(kb.dispatch("batch_status", {}, fk.call_tool))["batches"][0]["name"] == "t"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name}: OK")
