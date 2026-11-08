"""MCP server for Vast.ai GPU rental — a second, SSH-free compute backend
alongside colab_mcp. See docs/vast-mcp-plan.md for the full design
rationale; this is Phase 1 from that plan (REST API only, no SSH).

Talks to the Vast.ai REST API directly via stdlib urllib (the same
lightweight approach telegram.py's TelegramAPI already uses in this repo)
rather than depending on the `vastai` PyPI package — that package pins
cryptography==49.0.0, which hard-conflicts with this project's own
cryptography>=50.0.0 (used by the BYOK vault, crypto.py). Endpoint shapes
below were confirmed by reading the real `vastai` package source (v1.7.0),
not guessed at.

Tools:
  vast_search_offers — query the GPU marketplace
  vast_new           — rent an instance from a specific offer
  vast_status        — show one instance (or the active one)
  vast_sessions      — list all account instances
  vast_execute       — run a shell command (synchronous, ~20s budget)
  vast_upload        — push a small local file onto the instance (base64)
  vast_install       — pip install via vast_execute
  vast_stop          — halt GPU billing, keep storage (resumable)
  vast_destroy       — permanently terminate (stops ALL billing)

Auth: VAST_API_KEY env var — set via mcp.json's ${VAULT:vast} (same BYOK
pattern as FRED_API_KEY/KAGGLE_API_TOKEN), a Bearer token. Get a key at
https://console.vast.ai/manage-keys/ and store it as the "vast" BYOK
provider in the web UI (Settings -> BYOK keys).
"""
from __future__ import annotations
import base64
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from mcp.server import MCPServer

LOG = logging.getLogger("vast-mcp")
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

VAST_API_KEY = os.environ.get("VAST_API_KEY", "").strip()
VAST_BASE_URL = os.environ.get("VAST_URL", "https://console.vast.ai").rstrip("/")

server = MCPServer("vast-mcp")

# Instance states that will NEVER reach "running" on their own — confirmed
# in the real API's own docs (see docs/vast-mcp-plan.md §3). A poll loop
# must treat these as terminal failures, not "still starting", or it spins
# forever while storage charges accrue.
_TERMINAL_FAILURE_STATES = {"exited", "unknown", "offline"}

# Per-MCP-process state (mirrors colab_server.py's `_state` pattern).
_state: dict[str, Any] = {
    "active_instance_id": None,
    "created_at": {},   # instance_id -> time.time() at creation, for cost-so-far
    "dph": {},           # instance_id -> $/hr, for cost-so-far
}


# ---------------- HTTP helpers ----------------

def _request(method: str, path: str, json_data: dict | None = None, timeout: float = 30) -> Any:
    if not VAST_API_KEY:
        raise RuntimeError(
            "No Vast.ai API key configured. Store one as the 'vast' BYOK "
            "provider (web UI: Settings -> BYOK keys) — get a key at "
            "https://console.vast.ai/manage-keys/."
        )
    if not path.startswith("/api/v"):
        path = "/api/v0" + path
    url = VAST_BASE_URL + path
    body = json.dumps(json_data).encode() if json_data is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={
            "Authorization": f"Bearer {VAST_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"Vast.ai API {method} {path} -> HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Vast.ai API {method} {path} unreachable: {e.reason}") from e
    return json.loads(raw) if raw else {}


def _poll_result_url(result_url: str, retries: int = 40, delay: float = 0.5) -> str:
    """Poll a command/logs `result_url` until its content is ready.

    ~20s total budget by default — matches the real API being built for
    quick commands, not long-running jobs (see module docstring / plan).
    """
    last_err: Exception | None = None
    for _ in range(retries):
        time.sleep(delay)
        try:
            with urllib.request.urlopen(result_url, timeout=10) as resp:
                if resp.status == 200:
                    return resp.read().decode("utf-8", "replace")
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            last_err = e  # not ready yet (or a transient blip) — keep polling
    raise TimeoutError(
        f"Result not ready after {retries * delay:.0f}s: {result_url} (last error: {last_err})"
    )


def _run_command(instance_id: int, command: str) -> str:
    res = _request("PUT", f"/instances/command/{instance_id}/", {"command": command})
    result_url = res.get("result_url")
    if not result_url:
        return json.dumps(res)
    return _poll_result_url(result_url)


def _resolve_instance_id(instance_id: int | None) -> int:
    if instance_id is not None:
        return instance_id
    active = _state.get("active_instance_id")
    if active is None:
        raise RuntimeError(
            "No instance_id given and no active instance. Call vast_new first, "
            "or pass instance_id explicitly (see vast_sessions for ids)."
        )
    return active


def _cost_so_far(instance_id: int) -> float | None:
    created_at = _state["created_at"].get(instance_id)
    dph = _state["dph"].get(instance_id)
    if created_at is None or dph is None:
        return None
    hours = (time.time() - created_at) / 3600
    return round(hours * dph, 4)


# ---------------- tools ----------------

@server.tool()
def vast_search_offers(
    gpu_name: str = "", num_gpus: int = 1, max_price: float = 0,
    min_reliability: float = 0, region: str = "", spot: bool = False,
    limit: int = 10,
) -> str:
    """Search the Vast.ai GPU marketplace. Returns up to `limit` offers
    (id, gpu_name, num_gpus, dph_total=$/hr, reliability, geolocation),
    ranked by overall value (price + reliability), cheapest-first ties
    broken by score. `gpu_name` examples: RTX_4090, A100_SXM4, H100_SXM.
    `max_price` filters dph_total <= that value ($/hr). `region` filters
    geolocation (e.g. "US", "EU"). Pass `spot=True` to search interruptible
    (bid) pricing instead of on-demand — NOTE: vast_new still rents at
    full on-demand price unless you ALSO pass a bid_price to it; searching
    spot offers alone does not get you the discount."""
    q: dict[str, Any] = {
        "verified": {"eq": True}, "external": {"eq": False},
        "rentable": {"eq": True}, "rented": {"eq": False},
        "order": [["score", "desc"]],
        "type": "bid" if spot else "on-demand",
        "limit": int(limit),
        "allocated_storage": 5.0,
    }
    if gpu_name:
        q["gpu_name"] = {"eq": gpu_name}
    if num_gpus:
        q["num_gpus"] = {"eq": int(num_gpus)}
    if max_price:
        q["dph_total"] = {"lte": float(max_price)}
    if min_reliability:
        q["reliability"] = {"gt": float(min_reliability)}
    if region:
        q["geolocation"] = {"eq": region}

    res = _request("POST", "/bundles/", q)
    offers = res.get("offers", [])
    out = [
        {
            "id": o.get("id"), "gpu_name": o.get("gpu_name"),
            "num_gpus": o.get("num_gpus"), "dph_total": o.get("dph_total"),
            "reliability": o.get("reliability"), "geolocation": o.get("geolocation"),
            "gpu_ram": o.get("gpu_ram"), "cpu_ram": o.get("cpu_ram"),
        }
        for o in offers
    ]
    return json.dumps(out, indent=2)


@server.tool()
def vast_new(
    offer_id: int, image: str = "pytorch/pytorch:@vastai-automatic-tag",
    disk_gb: float = 20, bid_price: float | None = None, timeout: float = 180,
) -> str:
    """Rent an instance from a SPECIFIC offer id (call vast_search_offers
    first and choose one — don't guess an id). Polls until the instance
    reaches `running` or hits a terminal failure state (exited/unknown/
    offline — these NEVER recover, per the real API's own docs). BILLING
    STARTS IMMEDIATELY at creation (storage) and again at `running` (GPU)
    — report the offer's dph_total ($/hr) back to the user, and remember
    to call vast_destroy when done (vast_stop alone still bills storage).
    Pass `bid_price` ($/hr) to actually get interruptible/spot pricing —
    omitting it rents at full on-demand price even for a spot-search offer.
    """
    body: dict[str, Any] = {"image": image, "disk": disk_gb, "ssh": True, "direct": True}
    if bid_price is not None:
        body["price"] = bid_price

    res = _request("PUT", f"/asks/{offer_id}/", body)
    if not res.get("success"):
        return json.dumps({"error": "instance creation failed", "response": res})
    instance_id = res["new_contract"]

    deadline = time.time() + timeout
    status = None
    while time.time() < deadline:
        inst = _request("GET", f"/instances/{instance_id}/").get("instances") or {}
        status = inst.get("actual_status")
        if status == "running":
            _state["active_instance_id"] = instance_id
            _state["created_at"][instance_id] = time.time()
            _state["dph"][instance_id] = inst.get("dph_total")
            return json.dumps({
                "status": "running", "instance_id": instance_id,
                "dph_total": inst.get("dph_total"), "gpu_name": inst.get("gpu_name"),
                "note": "Remember to vast_destroy when done — billing continues otherwise.",
            }, indent=2)
        if status in _TERMINAL_FAILURE_STATES:
            return json.dumps({
                "error": f"instance reached terminal state {status!r} and will never start",
                "instance_id": instance_id,
                "note": "Destroying it now to stop storage billing.",
                "destroy_result": _request("DELETE", f"/instances/{instance_id}/"),
            })
        time.sleep(3)

    return json.dumps({
        "error": f"timed out after {timeout}s waiting for running (last status: {status})",
        "instance_id": instance_id,
        "note": "Instance was NOT destroyed — check vast_status and vast_destroy manually if unwanted.",
    })


@server.tool()
def vast_status(instance_id: int | None = None) -> str:
    """Show one instance's status, price, and estimated cost-so-far. Omit
    instance_id to show the currently active one (from the last vast_new)."""
    iid = _resolve_instance_id(instance_id)
    inst = _request("GET", f"/instances/{iid}/").get("instances") or {}
    return json.dumps({
        "instance_id": iid,
        "status": inst.get("actual_status"),
        "dph_total": inst.get("dph_total"),
        "gpu_name": inst.get("gpu_name"),
        "num_gpus": inst.get("num_gpus"),
        "estimated_cost_so_far_usd": _cost_so_far(iid),
    }, indent=2)


@server.tool()
def vast_sessions() -> str:
    """List all of the account's instances (any status)."""
    res = _request("GET", "/instances/")
    instances = res.get("instances", [])
    out = [
        {
            "instance_id": i.get("id"), "status": i.get("actual_status"),
            "gpu_name": i.get("gpu_name"), "dph_total": i.get("dph_total"),
            "estimated_cost_so_far_usd": _cost_so_far(i.get("id")),
        }
        for i in instances
    ]
    return json.dumps(out, indent=2)


@server.tool()
def vast_execute(command: str, instance_id: int | None = None, timeout: float = 20) -> str:
    """Run a shell command on the instance and return its output. This is
    SYNCHRONOUS with a short budget (~20s) — it is NOT meant for long jobs.
    For a training run or anything that takes minutes/hours, start it
    detached and poll: first call
    vast_execute("nohup python train.py > /workspace/train.log 2>&1 &"),
    then later vast_execute("tail -100 /workspace/train.log") to check
    progress — do not expect one vast_execute call to block until a long
    job finishes."""
    iid = _resolve_instance_id(instance_id)
    poll_retries = max(1, int(timeout / 0.5))
    res = _request("PUT", f"/instances/command/{iid}/", {"command": command})
    result_url = res.get("result_url")
    if not result_url:
        return json.dumps(res)
    return _poll_result_url(result_url, retries=poll_retries)


@server.tool()
def vast_upload(local_path: str, remote_path: str = "", instance_id: int | None = None) -> str:
    """Upload a SMALL local file (this session's own workspace) onto the
    instance via base64 — no SSH needed, but keep this to config/small
    data files (a few MB at most); for a large dataset, prefer downloading
    it directly ON the instance with vast_execute("wget/curl ...") from a
    URL instead of routing bytes through this tool. remote_path defaults
    to the local file's basename in the instance's home/workdir."""
    if not os.path.isfile(local_path):
        return json.dumps({"error": f"local file not found: {local_path}"})
    iid = _resolve_instance_id(instance_id)
    remote_path = remote_path or os.path.basename(local_path)
    with open(local_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    # Write via a single python3 -c invocation over the same execute API —
    # keeps this tool entirely SSH-free (see module docstring).
    write_cmd = (
        "python3 -c \"import base64,pathlib; "
        f"p=pathlib.Path({remote_path!r}); p.parent.mkdir(parents=True, exist_ok=True); "
        f"p.write_bytes(base64.b64decode({b64!r}))\""
    )
    out = _run_command(iid, write_cmd)
    return json.dumps({"status": "uploaded", "remote_path": remote_path, "output": out})


@server.tool()
def vast_install(packages: list[str], instance_id: int | None = None) -> str:
    """pip install packages on the instance."""
    iid = _resolve_instance_id(instance_id)
    cmd = "pip install " + " ".join(packages)
    out = _run_command(iid, cmd)
    return json.dumps({"instance_id": iid, "output": out})


@server.tool()
def vast_stop(instance_id: int | None = None) -> str:
    """Stop the instance: halts GPU billing, but STORAGE BILLING CONTINUES
    until vast_destroy. Resumable later (state/disk preserved) — use
    vast_destroy instead if you're actually done."""
    iid = _resolve_instance_id(instance_id)
    res = _request("PUT", f"/instances/{iid}/", {"state": "stopped"})
    return json.dumps({"instance_id": iid, "status": "stopped", "response": res,
                        "note": "Storage billing continues. Use vast_destroy to stop ALL billing."})


@server.tool()
def vast_destroy(instance_id: int | None = None) -> str:
    """Permanently terminate the instance — stops ALL billing (GPU and
    storage). Irreversible."""
    iid = _resolve_instance_id(instance_id)
    res = _request("DELETE", f"/instances/{iid}/")
    if _state.get("active_instance_id") == iid:
        _state["active_instance_id"] = None
    _state["created_at"].pop(iid, None)
    _state["dph"].pop(iid, None)
    return json.dumps({"instance_id": iid, "status": "destroyed", "response": res})


def main() -> None:
    import asyncio
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
