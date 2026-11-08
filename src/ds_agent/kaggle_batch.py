"""Batch runner for Kaggle notebooks: a persistent queue on top of Kaggle's MCP tools.

Kaggle runs at most five notebooks per account at once, each for up to about
12 hours. Long jobs (backtests, sweeps) are split over many notebooks, and
pushing, polling, downloading outputs and checking logs by hand loses work:
a notebook pushed while five are running sits queued, an output nobody
downloads expires, a shard that died half way looks like a finished one.

A *batch* is a named queue of jobs, persisted as one JSON file per batch in
``KAGGLE_BATCH_DIR`` (default ``~/.kaggle_batch``). A job is one notebook
(a Kaggle *script* kernel): its slug, its source (inline text or a file
path), its state and what its logs said.

Job states::

    queued -> running -> complete | error | cancelled -> collected

- ``queued``: not pushed yet (or requeued by ``retry``).
- ``running``: pushed; Kaggle reports it queued or running.
- ``complete`` / ``error`` / ``cancelled``: Kaggle says the session ended;
  outputs not downloaded yet (a failed download is retried next ``step``).
- ``collected``: outputs downloaded to the job's ``out_dir`` and its logs
  scanned. ``outcome`` then says ``ok`` (every log has the done marker, none
  has the fail or deadline marker), ``failures``, ``no-logs`` or ``error``
  (Kaggle reported an error).

``step`` is the only call that talks to Kaggle on its own: it refreshes the
running jobs, collects finished ones and pushes queued ones while fewer than
``max_running`` jobs are running *across all batches in the directory* (so
two batches never push a sixth notebook together). Run it every few minutes,
or whenever you want to know where a batch is.

Every function that talks to Kaggle takes ``call_tool(name, arguments)``, an
async callable for Kaggle's MCP tools. The kaggle proxy passes its upstream
session's ``call_tool``; a command-line client can pass a direct JSON-RPC
client (``JsonRpcKaggle`` below). Results may be an ``mcp`` CallToolResult,
a raw JSON-RPC ``result`` dict or an already-parsed payload.

The account token never enters a batch file or a result.
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import ssl
import time
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

try:  # package import (ds-agent)
    from .kaggle_requests import _fix_save_notebook
except ImportError:  # flat import (a vendored copy next to this file)
    from kaggle_requests import _fix_save_notebook  # type: ignore[no-redef]

CallTool = Callable[[str, dict[str, Any]], Awaitable[Any]]
Downloader = Callable[[str, Path], None]

ACTIVE = ("queued", "running")  # states whose (contract, session)-style claims are live
ENDED = ("complete", "error", "cancelled")
# Kaggle's session statuses (case varies by endpoint) mapped to job states.
_KAGGLE_STATE = {
    "queued": "running",
    "running": "running",
    "new_script": "running",
    "complete": "complete",
    "error": "error",
    "cancelrequested": "running",
    "cancelacknowledged": "cancelled",
}

DEFAULTS = {
    "max_running": 5,
    "log_glob": "*.log",
    "download_glob": "*",
    "done_regex": None,
    "fail_regex": None,
    "deadline_regex": None,
    "private": True,
    "internet": True,
}


class KaggleToolError(RuntimeError):
    """A Kaggle MCP tool answered with an error."""


class BatchError(ValueError):
    """A batch request that cannot be applied (unknown batch or job, bad state)."""


# --------------------------------------------------------------- storage

def batch_dir() -> Path:
    return Path(os.environ.get("KAGGLE_BATCH_DIR") or Path.home() / ".kaggle_batch").expanduser()


def _path(name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name):
        raise BatchError(f"bad batch name {name!r}: letters, digits, _ . - only")
    return batch_dir() / f"{name}.json"


def load(name: str) -> dict[str, Any]:
    p = _path(name)
    if not p.exists():
        raise BatchError(f"no batch {name!r} in {batch_dir()}")
    return json.loads(p.read_text())


def save(batch: dict[str, Any]) -> None:
    p = _path(batch["name"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(batch, indent=1))
    tmp.replace(p)


def list_batches() -> list[dict[str, Any]]:
    d = batch_dir()
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))] if d.exists() else []


def _job(batch: dict[str, Any], slug: str) -> dict[str, Any]:
    for j in batch["jobs"]:
        if j["slug"] == slug:
            return j
    raise BatchError(f"batch {batch['name']!r} has no job {slug!r}")


def _check_slug(slug: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,48}[a-z0-9]", slug):
        raise BatchError(f"bad slug {slug!r}: 3-50 chars, lowercase letters, digits and '-'")


def _new_job(spec: dict[str, Any], out_root: Path) -> dict[str, Any]:
    slug = spec["slug"]
    _check_slug(slug)
    if bool(spec.get("script")) == bool(spec.get("script_path")):
        raise BatchError(f"job {slug!r}: give exactly one of script / script_path")
    if spec.get("script_path") and not Path(spec["script_path"]).is_file():
        raise BatchError(f"job {slug!r}: no file {spec['script_path']}")
    return {
        "slug": slug,
        "script": spec.get("script"),
        "script_path": str(Path(spec["script_path"]).resolve()) if spec.get("script_path") else None,
        "out_dir": str(Path(spec.get("out_dir") or out_root / slug).resolve()),
        "status": "queued",
        "attempts": 0,
        "pushed_at": None,
        "finished_at": None,
        "kaggle_status": None,
        "kernel_session_id": None,
        "last_error": None,
        "outcome": None,
        "logs": {},
        "history": [],
        "meta": spec.get("meta") or {},
    }


def _active_slugs(skip: str | None = None) -> dict[str, str]:
    """slug -> batch name, for jobs queued or running in any batch."""
    out = {}
    for b in list_batches():
        if b["name"] == skip:
            continue
        for j in b["jobs"]:
            if j["status"] in ACTIVE:
                out[j["slug"]] = b["name"]
    return out


def create_batch(
    name: str,
    jobs: list[dict[str, Any]],
    *,
    owner: str,
    out_root: str | None = None,
    replace: bool = False,
    **options: Any,
) -> dict[str, Any]:
    """Create a batch of queued jobs. Pushes nothing (``step`` does).

    ``jobs``: ``[{"slug", "script" | "script_path", "out_dir"?, "meta"?}]``.
    ``options``: any of ``DEFAULTS`` (``max_running``, ``log_glob``,
    ``download_glob``, ``done_regex``, ``fail_regex``, ``deadline_regex``,
    ``private``, ``internet``).
    """
    unknown = set(options) - set(DEFAULTS)
    if unknown:
        raise BatchError(f"unknown options {sorted(unknown)}")
    if _path(name).exists() and not replace:
        raise BatchError(f"batch {name!r} exists (pass replace=True to overwrite it)")
    if not owner:
        raise BatchError("owner (the Kaggle user name) is required")
    for rx in ("done_regex", "fail_regex", "deadline_regex"):
        if options.get(rx):
            re.compile(options[rx])
    root = Path(out_root) if out_root else batch_dir() / name
    built = [_new_job(s, root) for s in jobs]
    slugs = [j["slug"] for j in built]
    if len(set(slugs)) != len(slugs):
        raise BatchError("duplicate slugs in one batch")
    clash = {s: b for s, b in _active_slugs(skip=name if replace else None).items() if s in slugs}
    if clash:
        raise BatchError(f"slugs already queued or running in other batches: {clash}")
    batch = {"name": name, "owner": owner, "created_at": _now(), "options": {**DEFAULTS, **options},
             "jobs": built}
    save(batch)
    return summary(batch)


def add_jobs(name: str, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """Append queued jobs to an existing batch (same checks as ``create_batch``)."""
    batch = load(name)
    root = Path(batch["jobs"][0]["out_dir"]).parent if batch["jobs"] else batch_dir() / name
    built = [_new_job(s, root) for s in jobs]
    have = {j["slug"] for j in batch["jobs"]}
    dup = [j["slug"] for j in built if j["slug"] in have]
    if dup or len({j["slug"] for j in built}) != len(built):
        raise BatchError(f"slugs already in batch {name!r} or duplicated: {dup}")
    clash = {s: b for s, b in _active_slugs(skip=name).items() if s in {j['slug'] for j in built}}
    if clash:
        raise BatchError(f"slugs already queued or running in other batches: {clash}")
    batch["jobs"] += built
    save(batch)
    return summary(batch)


def update_job(name: str, slug: str, **meta: Any) -> dict[str, Any]:
    """Merge caller-owned fields into a job's ``meta`` (the runner never reads them)."""
    batch = load(name)
    job = _job(batch, slug)
    job["meta"].update(meta)
    save(batch)
    return job


# --------------------------------------------------------------- talking to Kaggle

def _payload(res: Any) -> Any:
    """The JSON payload of a Kaggle tool result, raising on a tool error."""
    if hasattr(res, "content"):
        is_error = bool(getattr(res, "is_error", False) or getattr(res, "isError", False))
        content = res.content
        text = getattr(content[0], "text", "") if content else ""
    elif isinstance(res, dict) and "content" in res:
        is_error = bool(res.get("isError") or res.get("is_error"))
        content = res["content"]
        text = content[0].get("text", "") if content else ""
    else:
        return res
    if is_error:
        raise KaggleToolError(text[:500])
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {"text": text}


async def _status(call_tool: CallTool, owner: str, slug: str) -> str:
    res = _payload(await call_tool("get_notebook_session_status",
                                   {"request": {"userName": owner, "kernelSlug": slug}}))
    return str(res.get("status", "")).lower()


async def _outputs(call_tool: CallTool, owner: str, slug: str) -> list[dict[str, str]]:
    files, token = [], None
    for _ in range(50):  # pages
        req = {"userName": owner, "kernelSlug": slug, "pageSizeNullable": 100}
        if token:
            req["pageTokenNullable"] = token
        res = _payload(await call_tool("list_notebook_session_output", {"request": req}))
        files += res.get("files") or []
        token = res.get("nextPageToken") or res.get("next_page_token")
        if not token:
            break
    return files


def _session_id(payload: Any) -> int | None:
    """A kernel session id, if a Kaggle answer carries one."""
    if isinstance(payload, dict):
        for k in ("kernelSessionId", "kernel_session_id", "versionId", "version_id"):
            if isinstance(payload.get(k), int):
                return payload[k]
        for v in payload.values():
            found = _session_id(v)
            if found:
                return found
    if isinstance(payload, str):
        m = re.search(r"/kf/(\d+)/", payload)
        if m:
            return int(m.group(1))
    return None


def _ssl_context() -> ssl.SSLContext:
    cafile = os.environ.get("SSL_CERT_FILE")
    if not cafile and os.path.exists("/root/.ccr/ca-bundle.crt"):
        cafile = "/root/.ccr/ca-bundle.crt"
    return ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()


def http_download(url: str, dest: Path) -> None:
    """Fetch a signed Kaggle output url to ``dest`` through the env's HTTPS proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(),
                                         urllib.request.HTTPSHandler(context=_ssl_context()))
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    with opener.open(url, timeout=600) as r, open(part, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    part.replace(dest)


def _source(job: dict[str, Any]) -> str:
    return job["script"] if job["script"] is not None else Path(job["script_path"]).read_text()


async def _push(call_tool: CallTool, batch: dict[str, Any], job: dict[str, Any]) -> None:
    opts = batch["options"]
    req = _fix_save_notebook({
        "slug": f"{batch['owner']}/{job['slug']}", "newTitle": job["slug"], "text": _source(job),
        "kernelType": "script", "language": "python", "isPrivate": bool(opts["private"]),
        "enableInternet": bool(opts["internet"]), "enableGpu": False,
        "kernelExecutionType": "SaveAndRunAll",
    })
    res = _payload(await call_tool("save_notebook", {"request": req}))
    if isinstance(res, dict) and res.get("error"):
        raise KaggleToolError(str(res["error"])[:500])
    job["kernel_session_id"] = _session_id(res)
    job["push_response"] = (json.dumps(res) if not isinstance(res, str) else res)[:400]


def scan_logs(out_dir: Path, options: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Per log file: done / fail lines / deadline; and the job's outcome."""
    logs: dict[str, Any] = {}
    done_rx = re.compile(options["done_regex"]) if options.get("done_regex") else None
    fail_rx = re.compile(options["fail_regex"]) if options.get("fail_regex") else None
    dl_rx = re.compile(options["deadline_regex"]) if options.get("deadline_regex") else None
    for p in sorted(out_dir.rglob("*")) if out_dir.exists() else []:
        if not p.is_file() or not fnmatch.fnmatch(p.name, options["log_glob"]):
            continue
        text = p.read_text(errors="replace")
        fails = [ln for ln in text.splitlines() if fail_rx and fail_rx.search(ln)]
        logs[str(p.relative_to(out_dir))] = {
            "done": bool(done_rx.search(text)) if done_rx else None,
            "fail_lines": len(fails),
            "fail_examples": fails[:3],
            "deadline": bool(dl_rx.search(text)) if dl_rx else False,
            "last_line": (text.strip().splitlines() or [""])[-1][:200],
        }
    if not logs:
        return logs, "no-logs"
    bad = any(v["fail_lines"] or v["deadline"] or v["done"] is False for v in logs.values())
    return logs, "failures" if bad else "ok"


async def _collect(call_tool: CallTool, batch: dict[str, Any], job: dict[str, Any],
                   download: Downloader) -> None:
    out = Path(job["out_dir"])
    files = await _outputs(call_tool, batch["owner"], job["slug"])
    pattern = batch["options"]["download_glob"]
    for f in files:
        name = f.get("file_name") or f.get("fileName") or f.get("name")
        if not name or not f.get("url") or not fnmatch.fnmatch(name, pattern):
            continue
        dest = out / name
        if Path(name).is_absolute() or ".." in Path(name).parts:
            continue  # never write outside out_dir
        await asyncio.to_thread(download, f["url"], dest)
        if job["kernel_session_id"] is None:
            job["kernel_session_id"] = _session_id(f["url"])
    job["logs"], outcome = scan_logs(out, batch["options"])
    job["outcome"] = "error" if job["status"] == "error" else outcome
    job["downloaded"] = len(files)
    job["status"] = "collected"


def _running_everywhere(batch: dict[str, Any]) -> int:
    n = sum(j["status"] == "running" for j in batch["jobs"])
    for b in list_batches():
        if b["name"] != batch["name"]:
            n += sum(j["status"] == "running" for j in b["jobs"])
    return n


async def step(name: str, call_tool: CallTool, *, download: Downloader = http_download) -> dict[str, Any]:
    """Refresh, collect and push. Returns the batch summary plus this step's events."""
    batch = load(name)
    events: list[str] = []
    for job in batch["jobs"]:
        if job["status"] != "running":
            continue
        try:
            ks = await _status(call_tool, batch["owner"], job["slug"])
        except KaggleToolError as exc:
            job["last_error"] = f"status: {exc}"
            events.append(f"{job['slug']}: status check failed: {exc}")
            continue
        job["kaggle_status"] = ks
        state = _KAGGLE_STATE.get(ks.replace("_", "").replace("kernelworkerstatus.", ""), "running")
        if state != "running":
            job["status"], job["finished_at"] = state, _now()
            events.append(f"{job['slug']}: {ks}")
    save(batch)
    for job in batch["jobs"]:
        if job["status"] not in ENDED:
            continue
        try:
            await _collect(call_tool, batch, job, download)
            events.append(f"{job['slug']}: collected, outcome {job['outcome']}")
        except Exception as exc:  # a failed download is retried next step
            job["last_error"] = f"collect: {exc!r}"[:500]
            events.append(f"{job['slug']}: collect failed (retried next step): {exc!r}"[:300])
        save(batch)
    cap = int(batch["options"]["max_running"])
    for job in batch["jobs"]:
        if job["status"] != "queued":
            continue
        if _running_everywhere(batch) >= cap:
            events.append(f"{cap} jobs running across batches: the rest wait")
            break
        try:
            await _push(call_tool, batch, job)
        except Exception as exc:
            job["last_error"] = f"push: {exc!r}"[:500]
            events.append(f"{job['slug']}: push failed, stays queued: {exc!r}"[:300])
            save(batch)
            break  # do not hammer Kaggle with the rest
        job["status"], job["attempts"], job["pushed_at"] = "running", job["attempts"] + 1, _now()
        job["last_error"] = None
        events.append(f"{job['slug']}: pushed (attempt {job['attempts']})")
        save(batch)
    return {**summary(batch), "events": events}


def summary(batch: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for j in batch["jobs"]:
        counts[j["status"]] = counts.get(j["status"], 0) + 1
    return {
        "name": batch["name"],
        "owner": batch["owner"],
        "counts": counts,
        "jobs": [{k: j[k] for k in ("slug", "status", "outcome", "attempts", "kaggle_status",
                                    "pushed_at", "finished_at", "last_error", "out_dir")}
                 | {"logs": {n: {k: v[k] for k in ("done", "fail_lines", "deadline")}
                             for n, v in j["logs"].items()}}
                 for j in batch["jobs"]],
    }


def status(name: str | None = None) -> dict[str, Any]:
    """One batch's summary, or every batch's counts. No network."""
    if name:
        return summary(load(name))
    return {"batches": [{"name": b["name"], "counts": summary(b)["counts"]} for b in list_batches()]}


def retry(name: str, slug: str) -> dict[str, Any]:
    """Requeue an ended or collected job; its next push is a new version of the same notebook."""
    batch = load(name)
    job = _job(batch, slug)
    if job["status"] in ACTIVE:
        raise BatchError(f"{slug} is {job['status']}; only an ended or collected job can be retried")
    job["history"].append({k: job[k] for k in ("attempts", "pushed_at", "finished_at", "kaggle_status",
                                                "outcome", "logs")})
    job.update(status="queued", finished_at=None, kaggle_status=None, outcome=None, logs={},
               kernel_session_id=None, last_error=None)
    save(batch)
    return summary(batch)


async def cancel(name: str, slug: str, call_tool: CallTool) -> dict[str, Any]:
    """Cancel a job: drop it if queued, ask Kaggle to stop it if running."""
    batch = load(name)
    job = _job(batch, slug)
    if job["status"] == "queued":
        job["status"] = "cancelled"
        job["outcome"] = "cancelled before push"
        job["status"] = "collected"
        save(batch)
        return {"slug": slug, "result": "dropped from the queue (never pushed)"}
    if job["status"] != "running":
        return {"slug": slug, "result": f"not running ({job['status']}); nothing to cancel"}
    sid = job["kernel_session_id"]
    if sid is None:  # outputs written so far carry the session id in their urls
        try:
            for f in await _outputs(call_tool, batch["owner"], slug):
                sid = _session_id(f.get("url", ""))
                if sid:
                    break
        except KaggleToolError:
            pass
    if sid is None:
        return {"slug": slug, "result": "no kernel session id known yet (Kaggle reports it with the "
                "first output file); cancel from kaggle.com or try again later"}
    res = _payload(await call_tool("cancel_notebook_session", {"request": {"kernelSessionId": sid}}))
    job["kernel_session_id"] = sid
    save(batch)
    return {"slug": slug, "kernel_session_id": sid, "result": res}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------- a client without `mcp`

class JsonRpcKaggle:
    """Kaggle's MCP endpoint over plain HTTPS JSON-RPC (token from the environment).

    ``await client.call_tool(name, arguments)`` returns the raw ``result``
    dict, which ``_payload`` understands. For command-line use where the
    ``mcp`` package is not installed.
    """

    URL = "https://www.kaggle.com/mcp"

    def __init__(self, token_env: str = "KAGGLE_API_TOKEN") -> None:
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise RuntimeError(f"{token_env} not set")
        self._headers = {"Content-Type": "application/json",
                         "Accept": "application/json, text/event-stream",
                         "Authorization": f"Bearer {token}"}
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler(),
                                                   urllib.request.HTTPSHandler(context=_ssl_context()))
        self._id = 0
        self._ready = False

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.URL, body, self._headers)
        data = self._opener.open(req, timeout=180).read().decode()
        lines = [ln[5:] for ln in data.splitlines() if ln.startswith("data:")]
        msg = json.loads(lines[0] if lines else data)
        if "error" in msg:
            raise KaggleToolError(str(msg["error"])[:500])
        return msg["result"]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._ready:
            await asyncio.to_thread(self._rpc, "initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "kaggle-batch", "version": "1"}})
            self._ready = True
        return await asyncio.to_thread(self._rpc, "tools/call", {"name": name, "arguments": arguments})


# --------------------------------------------------------------- MCP tool definitions

_JOB_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "notebook slug: lowercase letters, digits, '-'"},
        "script": {"type": "string", "description": "the notebook's Python source"},
        "script_path": {"type": "string", "description": "or a local file holding it"},
        "out_dir": {"type": "string", "description": "where its outputs go (default <out_root>/<slug>)"},
        "meta": {"type": "object", "description": "caller's own fields, kept with the job"},
    },
    "required": ["slug"],
}

TOOLS: list[dict[str, Any]] = [
    {"name": "batch_create",
     "description": "Create a named batch of Kaggle script notebooks to run, at most max_running (default 5) "
                    "at once across all batches. Nothing is pushed until batch_step. Logs matching log_glob "
                    "are checked for done_regex / fail_regex / deadline_regex once a notebook finishes.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string"}, "owner": {"type": "string", "description": "Kaggle user name"},
         "jobs": {"type": "array", "items": _JOB_SCHEMA},
         "out_root": {"type": "string"}, "replace": {"type": "boolean"},
         "max_running": {"type": "integer"}, "log_glob": {"type": "string"},
         "download_glob": {"type": "string"}, "done_regex": {"type": "string"},
         "fail_regex": {"type": "string"}, "deadline_regex": {"type": "string"},
         "private": {"type": "boolean"}, "internet": {"type": "boolean"}},
         "required": ["name", "owner", "jobs"]}},
    {"name": "batch_step",
     "description": "Advance a batch: refresh running notebooks, download and check the outputs of finished "
                    "ones, push queued ones while under the running cap. Call it periodically.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "batch_status",
     "description": "A batch's jobs, states and log checks (no Kaggle call); without name, every batch's counts.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}}},
    {"name": "batch_retry",
     "description": "Requeue an ended or collected job of a batch; the next batch_step pushes it again.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "slug": {"type": "string"}},
                      "required": ["name", "slug"]}},
    {"name": "batch_cancel",
     "description": "Cancel a job: drop it if still queued, ask Kaggle to stop it if running.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}, "slug": {"type": "string"}},
                      "required": ["name", "slug"]}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


async def dispatch(name: str, args: dict[str, Any], call_tool: CallTool) -> dict[str, Any]:
    """Run one batch_* MCP tool."""
    if name == "batch_create":
        a = dict(args)
        opts = {k: a.pop(k) for k in list(a) if k in DEFAULTS}
        return create_batch(a["name"], a["jobs"], owner=a["owner"], out_root=a.get("out_root"),
                            replace=bool(a.get("replace", False)), **opts)
    if name == "batch_step":
        return await step(args["name"], call_tool)
    if name == "batch_status":
        return status(args.get("name"))
    if name == "batch_retry":
        return retry(args["name"], args["slug"])
    if name == "batch_cancel":
        return await cancel(args["name"], args["slug"], call_tool)
    raise BatchError(f"unknown batch tool {name!r}")
