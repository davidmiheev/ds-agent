"""Unit test for vast_mcp/server.py's request-building and polling logic.

No live Vast.ai account/API key needed — mocks urllib.request.urlopen to
return canned responses matching the real API's confirmed response shapes
(see docs/vast-mcp-plan.md, grounded in the real `vastai` package source).
Run with: PYTHONPATH=src python tests/test_vast_mcp_logic.py
"""
import io
import json
import sys
import time
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import vast_mcp.server as vm

vm.VAST_API_KEY = "test-key-123"


class _FakeResponse:
    def __init__(self, body: dict | str, status: int = 200):
        self._body = body if isinstance(body, str) else json.dumps(body)
        self.status = status

    def read(self):
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mock_urlopen(responses):
    """Returns a callable that pops one canned response per call, and
    records every Request object it was called with."""
    calls = []

    def _fn(req, timeout=None):
        calls.append(req)
        resp = responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return _FakeResponse(resp)

    _fn.calls = calls
    return _fn


# ---------------------------------------------------------- auth header ----

with mock.patch("urllib.request.urlopen", _mock_urlopen([{"ok": True}])) as m:
    vm._request("GET", "/instances/")
    req = m.calls[0]
    assert req.get_header("Authorization") == "Bearer test-key-123"
    assert req.full_url == "https://console.vast.ai/api/v0/instances/"
    assert req.get_method() == "GET"
print("_request sends Bearer auth header + correct URL/method: OK")

vm.VAST_API_KEY = ""
try:
    vm._request("GET", "/instances/")
    raise AssertionError("expected RuntimeError with no API key")
except RuntimeError as e:
    assert "No Vast.ai API key" in str(e)
print("_request without an API key raises a clear error instead of a bare 401: OK")
vm.VAST_API_KEY = "test-key-123"


# ------------------------------------------------------- search offers ----

with mock.patch("urllib.request.urlopen", _mock_urlopen([
    {"offers": [{"id": 1, "gpu_name": "RTX_4090", "num_gpus": 1, "dph_total": 0.35,
                 "reliability": 0.99, "geolocation": "US", "gpu_ram": 24, "cpu_ram": 32}]}
])) as m:
    out = json.loads(vm.vast_search_offers(gpu_name="RTX_4090", max_price=0.5, min_reliability=0.9))
    req = m.calls[0]
    body = json.loads(req.data)
    assert body["gpu_name"] == {"eq": "RTX_4090"}
    assert body["dph_total"] == {"lte": 0.5}
    assert body["reliability"] == {"gt": 0.9}
    assert body["type"] == "on-demand"
    assert req.selector == "/api/v0/bundles/" or req.full_url.endswith("/bundles/")
    assert out == [{"id": 1, "gpu_name": "RTX_4090", "num_gpus": 1, "dph_total": 0.35,
                     "reliability": 0.99, "geolocation": "US", "gpu_ram": 24, "cpu_ram": 32}]
print("vast_search_offers builds the real {field: {op: value}} query shape: OK")

with mock.patch("urllib.request.urlopen", _mock_urlopen([{"offers": []}])) as m:
    vm.vast_search_offers(spot=True)
    body = json.loads(m.calls[0].data)
    assert body["type"] == "bid"
print("vast_search_offers(spot=True) requests bid-type offers: OK")


# ------------------------------------------------------------- vast_new ----

with mock.patch("urllib.request.urlopen", _mock_urlopen([
    {"success": True, "new_contract": 555},   # PUT /asks/{id}/
    {"instances": {"actual_status": "loading", "dph_total": 0.35, "gpu_name": "RTX_4090"}},  # poll 1
    {"instances": {"actual_status": "running", "dph_total": 0.35, "gpu_name": "RTX_4090"}},   # poll 2
])) as m:
    with mock.patch("time.sleep"):  # don't actually wait between polls
        out = json.loads(vm.vast_new(offer_id=42, bid_price=0.2))
    assert out["status"] == "running"
    assert out["instance_id"] == 555
    create_req = m.calls[0]
    assert create_req.full_url == "https://console.vast.ai/api/v0/asks/42/"
    create_body = json.loads(create_req.data)
    assert create_body["price"] == 0.2, "bid_price must be forwarded so spot pricing actually applies"
    assert vm._state["active_instance_id"] == 555
    assert vm._state["dph"][555] == 0.35
print("vast_new polls until running, forwards bid_price, and records active instance: OK")

# Terminal failure state must stop polling immediately and destroy (not
# leave a storage-billing instance dangling) rather than looping forever.
vm._state["active_instance_id"] = None
with mock.patch("urllib.request.urlopen", _mock_urlopen([
    {"success": True, "new_contract": 556},
    {"instances": {"actual_status": "exited"}},
    {"success": True},  # the destroy call
])) as m:
    with mock.patch("time.sleep"):
        out = json.loads(vm.vast_new(offer_id=43))
    assert "error" in out
    assert out["instance_id"] == 556
    assert m.calls[-1].get_method() == "DELETE", "a terminal failure state must trigger an immediate destroy"
print("vast_new treats exited/unknown/offline as terminal and destroys instead of polling forever: OK")


# --------------------------------------------------------- vast_execute ----

vm._state["active_instance_id"] = 555
with mock.patch("urllib.request.urlopen") as m:
    m.side_effect = [
        _FakeResponse({"result_url": "https://console.vast.ai/result/abc"}),
        _FakeResponse("nvidia-smi output here"),
    ]
    with mock.patch("time.sleep"):
        out = vm.vast_execute("nvidia-smi")
    assert out == "nvidia-smi output here"
print("vast_execute posts a command and polls its result_url: OK")

# instance_id resolution: no active instance and none passed -> clear error.
vm._state["active_instance_id"] = None
try:
    vm.vast_execute("echo hi")
    raise AssertionError("expected RuntimeError with no active instance")
except RuntimeError as e:
    assert "No instance_id" in str(e)
print("vast_execute with no active/given instance raises instead of guessing: OK")


# ---------------------------------------------------------- vast_upload ----

vm._state["active_instance_id"] = 555
tmp = Path("/tmp") / "vast_mcp_test_upload.txt"
tmp.write_bytes(b"hello vast")
try:
    with mock.patch("urllib.request.urlopen", _mock_urlopen([
        {"result_url": "https://console.vast.ai/result/xyz"},
        "ok",
    ])) as m:
        with mock.patch("time.sleep"):
            out = json.loads(vm.vast_upload(str(tmp)))
    assert out["remote_path"] == tmp.name
    cmd_body = json.loads(m.calls[0].data)
    assert "base64" in cmd_body["command"]
    print("vast_upload defaults remote_path to basename and writes via base64 command: OK")

    missing = json.loads(vm.vast_upload("/no/such/file.bin"))
    assert "error" in missing
    print("vast_upload rejects a missing local file: OK")
finally:
    tmp.unlink()


# ------------------------------------------------------- stop / destroy ----

vm._state["active_instance_id"] = 555
vm._state["created_at"][555] = time.time()
vm._state["dph"][555] = 0.35
with mock.patch("urllib.request.urlopen", _mock_urlopen([{"success": True}])) as m:
    out = json.loads(vm.vast_destroy())
    assert m.calls[0].get_method() == "DELETE"
    assert out["instance_id"] == 555
assert vm._state["active_instance_id"] is None
assert 555 not in vm._state["created_at"]
print("vast_destroy clears active-instance state after a successful call: OK")

print("\nALL VAST MCP LOGIC CHECKS PASSED")
