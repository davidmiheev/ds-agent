"""Concurrent thread/process tests, no Kaggle credentials or network."""
import asyncio
import json
import multiprocessing
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ds_agent import kaggle_batch as kb


async def fake_tool(name, arguments):
    await asyncio.sleep(0.03)
    if name == "save_notebook":
        slug = arguments["request"]["slugNullable"]
        with (kb.batch_dir() / "pushed.log").open("a") as stream:
            stream.write(slug + "\n")
        return {"ref": slug}
    if name == "get_notebook_session_status":
        return {"status": "RUNNING"}
    raise AssertionError(name)


def worker(directory, batch, index, barrier):
    os.environ["KAGGLE_BATCH_DIR"] = directory
    barrier.wait(timeout=15)
    asyncio.run(kb.step(batch, fake_tool))
    kb.update_job("a", "job-a0", **{f"worker{index}": True})


def check_workers(kind, same_batch):
    with tempfile.TemporaryDirectory() as directory:
        os.environ["KAGGLE_BATCH_DIR"] = directory
        for batch in ("a", "b"):
            kb.create_batch(batch, [{"slug": f"job-{batch}{i}", "script": "print(1)"} for i in range(4)],
                            owner="fake", max_running=3)
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(5)
        args = [(directory, "a" if same_batch or i % 2 == 0 else "b", i, barrier) for i in range(4)]
        if kind == "threads":
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(worker, *a) for a in args]
                barrier.wait(timeout=15)
                for future in futures:
                    future.result(timeout=30)
        else:
            processes = [context.Process(target=worker, args=a) for a in args]
            for process in processes:
                process.start()
            barrier.wait(timeout=15)
            for process in processes:
                process.join(timeout=30)
                if process.is_alive():
                    process.terminate()
                    raise AssertionError("worker hung")
                assert process.exitcode == 0, process.exitcode
        pushed = (Path(directory) / "pushed.log").read_text().splitlines()
        assert len(pushed) == len(set(pushed)) == 3, pushed
        assert sum(j["status"] == "running" for b in kb.list_batches() for j in b["jobs"]) == 3
        assert len(kb.load("a")["jobs"][0]["meta"]) == 4
        assert not list(Path(directory).glob("*.tmp"))
        for path in Path(directory).glob("*.json"):
            json.loads(path.read_text())
    print(f"{kind}, {'same batch' if same_batch else 'different batches'}: cap, no duplicates, no lost updates: OK")


async def async_checks():
    with tempfile.TemporaryDirectory() as directory:
        os.environ["KAGGLE_BATCH_DIR"] = directory
        await asyncio.to_thread(kb.create_batch, "a",
                                [{"slug": "job-a0", "script": "print(1)"}], owner="fake")
        await asyncio.wait_for(asyncio.gather(
            kb.step("a", fake_tool),
            kb.dispatch("batch_create", {"name": "empty", "owner": "fake", "jobs": []}, fake_tool),
            kb.step("a", fake_tool)), timeout=5)
        assert (await kb.step("empty", fake_tool))["jobs"] == []
        try:
            await kb.step("missing", fake_tool)
        except kb.BatchError:
            pass
        else:
            raise AssertionError("missing batch accepted")
        entered = asyncio.Event()
        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(kb.step("a", blocked))
        await entered.wait()
        waiter = asyncio.create_task(kb.step("a", fake_tool))
        await asyncio.sleep(0.03)
        waiter.cancel()
        task.cancel()
        await asyncio.gather(task, waiter, return_exceptions=True)
        await asyncio.wait_for(kb.step("a", fake_tool), timeout=2)

    # Sync mutations called directly on the event loop would deadlock (blocking
    # flock while a lock-holder needs the loop); they must fail loudly instead.
    for fn, args in ((kb.retry, ("a", "job-a0")),
                     (kb.update_job, ("a", "job-a0")),
                     (kb.create_batch, ("c", []))):
        try:
            fn(*args)
        except kb.BatchError:
            pass
        else:
            raise AssertionError(f"{fn.__name__} on the loop did not fail fast")
    print("async dispatch, empty/error paths and cancellation release locks: OK")
    print("sync mutations on the event loop raise instead of deadlocking: OK")


if __name__ == "__main__":
    for kind in ("threads", "processes"):
        for same in (True, False):
            check_workers(kind, same)
    asyncio.run(async_checks())
