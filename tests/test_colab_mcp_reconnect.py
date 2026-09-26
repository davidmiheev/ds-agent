"""Regression test for three real bugs fixed in colab_server.py:

1. PKCE verifier discarded in _complete_oauth — a fresh Flow was built from
   just the client config, generating a different random code_verifier than
   the one whose code_challenge was already sent to Google in _start_oauth,
   so the token exchange would always fail (invalid_grant).
2. No reconnect path — the runtime proxy token expires at ~3600s
   independent of the runtime's actual lifetime, and nothing refreshed it;
   naively treating an expired token as a dead session would prune a live,
   still-billing runtime instead of just reconnecting to it.
3. upload -> exec path mismatch — colab_execute never chdir'd the kernel
   to /content (unlike the official CLI's exec/repl), so a file uploaded
   via the Contents API (always rooted at /content) could silently not be
   found by relative-path open() calls in colab_execute'd code.

Uses fakes/mocks throughout — no real Google OAuth or Colab runtime needed.
Run with: src/colab_mcp/.venv/bin/python tests/test_colab_mcp_reconnect.py
"""
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from colab_mcp import colab_server as cs
from colab_cli.client import Accelerator, Assignment, PostAssignmentResponse, RuntimeProxyInfo, Shape, Variant
from colab_cli.state import SessionState

# _complete_oauth() persists credentials to TOKEN_CONFIG_PATH. Point it at a
# throwaway file: with the real ~/.config/colab-cli/token.json, running this
# test silently replaced the user's real Colab token with the fake "{}" below.
import tempfile
_REAL_TOKEN_PATH = cs.TOKEN_CONFIG_PATH
_real_token_before = Path(_REAL_TOKEN_PATH).read_bytes() if Path(_REAL_TOKEN_PATH).exists() else None
_tmp_cfg = Path(tempfile.mkdtemp())
cs.TOKEN_CONFIG_PATH = str(_tmp_cfg / "token.json")
cs.PENDING_AUTH_PATH = str(_tmp_cfg / "pending_auth.json")


# ------------------------------------------------------------- 1. PKCE ------

class _FakeFlow:
    def __init__(self, code_verifier=None):
        self.code_verifier = code_verifier
        self.fetch_token_calls = []
        self.credentials = _FakeCreds()

    def authorization_url(self, **kwargs):
        # Mirrors the real Flow: only generates a verifier once a URL is built.
        if self.code_verifier is None:
            self.code_verifier = "verifier-from-start-oauth"
        return "https://accounts.google.com/fake-auth-url", "state"

    def fetch_token(self, **kwargs):
        self.fetch_token_calls.append(kwargs)


class _FakeCreds:
    def to_json(self):
        return "{}"


_flow_instances_built = []


def _fake_from_client_config(config, scopes, code_verifier=None):
    flow = _FakeFlow(code_verifier)
    _flow_instances_built.append(flow)
    return flow


class _FakeInstalledAppFlow:
    from_client_config = staticmethod(_fake_from_client_config)


# Patch the module google_auth_oauthlib.flow.InstalledAppFlow resolves to
# (both _start_oauth and _complete_oauth do `from google_auth_oauthlib.flow
# import InstalledAppFlow` locally, so patch the source module).
import google_auth_oauthlib.flow as _gaof
_real_installed_app_flow = _gaof.InstalledAppFlow
_gaof.InstalledAppFlow = _FakeInstalledAppFlow

# _start_oauth() also reads colab_cli's bundled oauth_config.json via
# importlib.resources — that's real static JSON on disk, no network, fine
# to leave untouched.

url = cs._start_oauth()
assert len(_flow_instances_built) == 1, "expected exactly one Flow built by _start_oauth"
started_flow = _flow_instances_built[0]
assert cs._state["_auth_flow"] is started_flow, "the Flow from _start_oauth must be persisted for _complete_oauth to reuse"
print("_start_oauth persists its Flow object: OK")

res = cs._complete_oauth("fake-auth-code")
assert len(_flow_instances_built) == 1, (
    "_complete_oauth must NOT build a second Flow (a fresh one would carry "
    "a different, never-exposed PKCE code_verifier)"
)
assert len(started_flow.fetch_token_calls) == 1
assert started_flow.fetch_token_calls[0].get("code") == "fake-auth-code"
assert res == {"status": "authenticated"}
assert cs._state["_auth_flow"] is None, "flow should be cleared after completion"
print("_complete_oauth reuses the SAME Flow (same PKCE code_verifier): OK")

assert not os.path.exists(cs.PENDING_AUTH_PATH), "persisted verifier must be removed after success"
assert oct(os.stat(cs.TOKEN_CONFIG_PATH).st_mode & 0o777) == "0o600", "token file holds a refresh token: must be 0600"
print("token file is written 0600: OK")
print("pending-auth file is cleared after a successful login: OK")

# Server restart between colab_auth() and colab_auth(code=...): the in-memory
# flow is gone, but the persisted verifier must let the SAME PKCE exchange finish.
_flow_instances_built.clear()
cs._start_oauth()
assert oct(os.stat(cs.PENDING_AUTH_PATH).st_mode & 0o777) == "0o600", "verifier file must be private"
cs._state["_auth_flow"] = None  # simulate the restart
res = cs._complete_oauth("code-after-restart")
assert res == {"status": "authenticated"}
rebuilt = _flow_instances_built[-1]
assert len(_flow_instances_built) == 2 and rebuilt.code_verifier == "verifier-from-start-oauth", (
    "after a restart the rebuilt Flow must carry the verifier from the URL that was handed out"
)
assert rebuilt.fetch_token_calls[0].get("code") == "code-after-restart"
print("_complete_oauth survives a server restart via the persisted PKCE verifier: OK")

# Calling colab_auth(code=...) with no pending flow must fail loudly, not
# silently build a fresh (PKCE-broken) flow.
cs._state["_auth_flow"] = None
try:
    cs._complete_oauth("some-code")
    raise AssertionError("expected RuntimeError with no pending flow")
except RuntimeError as e:
    assert "pending OAuth flow" in str(e)
print("_complete_oauth without a pending flow raises instead of silently re-flowing: OK")

# A persisted verifier older than the TTL is discarded (Google codes expire anyway).
cs._start_oauth()
cs._state["_auth_flow"] = None
_old = json.load(open(cs.PENDING_AUTH_PATH))
_old["created_at"] -= cs._PENDING_AUTH_TTL_S + 1
json.dump(_old, open(cs.PENDING_AUTH_PATH, "w"))
try:
    cs._complete_oauth("stale-code")
    raise AssertionError("expected RuntimeError for a stale pending flow")
except RuntimeError as e:
    assert "pending OAuth flow" in str(e)
assert not os.path.exists(cs.PENDING_AUTH_PATH), "stale verifier file must be removed"
print("stale persisted verifier is rejected and removed: OK")

_gaof.InstalledAppFlow = _real_installed_app_flow


# ------------------------------------------------------- 2. reconnect -------

class _FakeAssignClient:
    def __init__(self, response):
        self.response = response
        self.assign_calls = []

    def assign(self, notebook_hash, variant=None, accelerator=None, shape=None):
        self.assign_calls.append({
            "notebook_hash": notebook_hash, "variant": variant,
            "accelerator": accelerator, "shape": shape,
        })
        return self.response


class _FakeStore:
    def __init__(self):
        self.added = []
        self.removed = []

    def add(self, s):
        self.added.append(s)

    def remove(self, name):
        self.removed.append(name)

    def get(self, name):
        return None

    def list(self):
        return {}


orig_notebook_hash = uuid.uuid4()
s = SessionState(
    name="sess1", token="stale-token", url="https://old-proxy.example",
    endpoint="ep1", variant=Variant.GPU.value, accelerator=Accelerator.T4.value,
    machine_shape="STANDARD",
)
cs._state.update({
    "active_session": s,
    "runtime": object(),  # sentinel: must be cleared on refresh
    "notebook_hash": orig_notebook_hash,
    "variant": Variant.GPU, "accelerator": Accelerator.T4, "shape": Shape.STANDARD,
    "store": _FakeStore(),
    "client": None,
})

# 2a. Token still fresh -> no assign() call at all.
import time
cs._state["runtime_token_expires_at"] = time.time() + 3000  # 50 min left
fresh_client = _FakeAssignClient(response=None)
cs._get_client = lambda: fresh_client
cs._refresh_runtime_token_if_needed()
assert fresh_client.assign_calls == [], "must not refresh a token that's still fresh"
assert cs._state["runtime"] is not None, "runtime must be left alone when token is fresh"
print("fresh token: no unnecessary reconnect: OK")

# 2b. Token expired -> assign() called with the SAME notebook_hash, session
# state updated in place, runtime cleared for rebuild, nothing pruned.
new_proxy = RuntimeProxyInfo(token="fresh-token", tokenExpiresInSeconds=3600, url="https://new-proxy.example")
expired_response = Assignment(endpoint="ep1", runtimeProxyInfo=new_proxy)
expired_client = _FakeAssignClient(response=expired_response)
cs._get_client = lambda: expired_client
cs._state["runtime_token_expires_at"] = time.time() - 10  # already expired
cs._state["runtime"] = object()  # sentinel

cs._refresh_runtime_token_if_needed()

assert len(expired_client.assign_calls) == 1
assert expired_client.assign_calls[0]["notebook_hash"] == orig_notebook_hash, (
    "must reuse the ORIGINAL notebook_hash so the backend reconnects to "
    "the same live runtime instead of a client-side-only concept of identity"
)
assert cs._state["active_session"].token == "fresh-token"
assert cs._state["active_session"].url == "https://new-proxy.example"
assert cs._state["runtime"] is None, "runtime must be cleared so _active_runtime() rebuilds with the fresh token"
assert cs._state["store"].removed == [], (
    "an expired TOKEN must never prune the session record — the runtime "
    "itself may still be alive and billing"
)
assert s in cs._state["store"].added, "refreshed session state must be persisted"
print("expired token: reconnects via assign() with the SAME notebook_hash, never prunes: OK")


# ------------------------------------------------- 3. upload / chdir -------

class _FakeKernelRuntime:
    """Records execute_code calls; stands in for ColabRuntime."""
    _instances = []

    def __init__(self, url, token, kernel_id=None, session_id=None,
                 on_kernel_started=None, on_session_started=None):
        self.url = url
        self.token = token
        self.executed = []
        _FakeKernelRuntime._instances.append(self)

    def execute_code(self, code, timeout=None, output_hook=None):
        self.executed.append(code)
        return []


cs.ColabRuntime = _FakeKernelRuntime
cs._state.update({
    "active_session": s, "runtime": None,
    "runtime_token_expires_at": time.time() + 3000,  # fresh, no refresh triggered here
})

runtime = cs._active_runtime()
assert isinstance(runtime, _FakeKernelRuntime)
assert len(runtime.executed) == 1
assert "os.chdir('/content')" in runtime.executed[0], (
    "a freshly built runtime must chdir to /content — the same root the "
    "Contents API (colab_upload) always uses — or relative-path open() "
    "calls in colab_execute code can silently miss an uploaded file"
)
print("_active_runtime() chdir's a fresh kernel to /content: OK")

# A second call must NOT re-chdir (runtime already cached/built).
runtime2 = cs._active_runtime()
assert runtime2 is runtime
assert len(runtime.executed) == 1, "cached runtime must not re-issue the chdir prelude"
print("cached runtime is reused without re-chdir'ing: OK")


class _FakeContentsClient:
    calls = []
    dirs = []

    def __init__(self, session_state):
        self.session_state = session_state

    def upload(self, local_path, remote_path):
        _FakeContentsClient.calls.append((local_path, remote_path))

    def _request(self, method, path, params=None, json_data=None):
        assert method == "PUT" and json_data == {"type": "directory"}, (method, json_data)
        _FakeContentsClient.dirs.append(path)


import colab_cli.contents as _contents_mod
_real_contents_client = _contents_mod.ContentsClient
_contents_mod.ContentsClient = _FakeContentsClient

import tempfile, os
with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tf:
    tf.write(b"a,b\n1,2\n")
    local_tmp = tf.name

try:
    # remote_path defaults to the local file's basename.
    result = asyncio.run(cs.call_tool("colab_upload", {"local_path": local_tmp}))
    payload = result[0].text
    expected_name = os.path.basename(local_tmp)
    assert f'"remote_path": "{expected_name}"' in payload, payload
    # The Contents API is rooted at `/` on Colab (not /content — verified
    # against a live T4 runtime, and matching the official CLI's own
    # `install -r`, which uploads to "content/<name>"), so the API path
    # must carry the "content/" prefix for the file to land in the kernel's cwd.
    assert _FakeContentsClient.calls[-1] == (local_tmp, f"content/{expected_name}")
    print("colab_upload defaults remote_path to basename under content/: OK ->", payload[:120])

    # A leading slash on an explicit remote_path is stripped so it stays
    # under the SAME /content root colab_execute's cwd uses; missing parent
    # dirs are created first (a PUT under a missing dir is a bare HTTP 500).
    _FakeContentsClient.dirs.clear()
    result2 = asyncio.run(cs.call_tool("colab_upload", {"local_path": local_tmp, "remote_path": "/data/x.csv"}))
    payload2 = result2[0].text
    assert '"remote_path": "data/x.csv"' in payload2, payload2
    assert _FakeContentsClient.calls[-1] == (local_tmp, "content/data/x.csv")
    assert _FakeContentsClient.dirs == ["content", "content/data"], _FakeContentsClient.dirs
    print("colab_upload strips a leading slash and creates parent dirs: OK")

    # An explicit /content/ prefix is not doubled up.
    asyncio.run(cs.call_tool("colab_upload", {"local_path": local_tmp, "remote_path": "/content/y.csv"}))
    assert _FakeContentsClient.calls[-1] == (local_tmp, "content/y.csv"), _FakeContentsClient.calls[-1]
    print("colab_upload accepts an explicit /content/ prefix without doubling it: OK")

    # Paths escaping /content are rejected, nothing uploaded.
    before = len(_FakeContentsClient.calls)
    result_esc = asyncio.run(cs.call_tool("colab_upload", {"local_path": local_tmp, "remote_path": "../etc/x"}))
    assert "must name a file under /content" in result_esc[0].text, result_esc[0].text
    assert len(_FakeContentsClient.calls) == before
    print("colab_upload rejects paths escaping /content: OK")

    # Missing local file -> clean error, no upload attempted.
    before = len(_FakeContentsClient.calls)
    result3 = asyncio.run(cs.call_tool("colab_upload", {"local_path": "/no/such/file.csv"}))
    assert "not found" in result3[0].text
    assert len(_FakeContentsClient.calls) == before
    print("colab_upload rejects a missing local file without uploading: OK")
finally:
    os.unlink(local_tmp)
    _contents_mod.ContentsClient = _real_contents_client

# ------------------------------------------------ 4. execute outputs -------
# A real 1x1 PNG, base64 with a trailing newline exactly as Jupyter sends it.
_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==\n"
blocks = cs._output_blocks([
    {"output_type": "stream", "text": "hello\n"},
    {"output_type": "display_data", "data": {"text/plain": "<Figure size 640x480 with 1 Axes>", "image/png": _png}},
    {"output_type": "execute_result", "data": {"text/plain": "42"}},
    {"output_type": "display_data", "data": {"image/svg+xml": "<svg></svg>", "text/plain": "<svg>"}},
    {"output_type": "error", "ename": "ValueError", "evalue": "boom", "traceback": []},
])
kinds = [b.type for b in blocks]
assert kinds == ["text", "image", "text", "text", "text"], kinds
img = blocks[1]
assert img.mimeType == "image/png" and "\n" not in img.data, "image block must validate and carry clean base64"
import base64
assert base64.b64decode(img.data).startswith(b"\x89PNG"), "payload must still decode to the PNG"
assert "<Figure" not in " ".join(b.text for b in blocks if b.type == "text"), "figure repr is noise next to the image"
assert "SVG output" in blocks[3].text and "ValueError: boom" in blocks[4].text
print("colab_execute outputs -> MCP blocks (image validates, base64 cleaned, svg/errors handled): OK")

_real_token_after = Path(_REAL_TOKEN_PATH).read_bytes() if Path(_REAL_TOKEN_PATH).exists() else None
assert _real_token_after == _real_token_before, "test must never touch the real Colab token file"
print("real ~/.config/colab-cli/token.json left untouched: OK")

print("\nALL COLAB MCP RECONNECT/PKCE/UPLOAD CHECKS PASSED")
