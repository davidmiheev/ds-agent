"""Unit test for ds_agent/kaggle_mcp.py's request auto-correction logic.

No network / no real Kaggle server needed — these are pure functions.
Guards against regressing the exact bugs documented in docs/debug_notes.md
(2026-09-06 kaggle entries): plain field names silently ignored (need the
`Nullable` suffix), and `kernelType` confused with `language`.
Run with: PYTHONPATH=src python tests/test_kaggle_mcp_proxy.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ds_agent.kaggle_mcp import _fix_save_notebook, _nullable_suffixed

# The exact naive call an unguided model makes: plain names, and a language
# name mistakenly used as the kernel type.
fixed = _fix_save_notebook({
    "newTitle": "x", "text": "print(1)", "isPrivate": True,
    "kernelType": "python", "language": "python",
})
assert fixed == {
    "newTitleNullable": "x", "textNullable": "print(1)", "isPrivateNullable": True,
    "kernelTypeNullable": "script", "languageNullable": "python",
}, fixed
print("naive plain-name + confused kernelType call: OK ->", fixed)

# Already-correct calls pass through unchanged (not just defaulted to script/python).
already_ok = _fix_save_notebook({
    "newTitleNullable": "x", "textNullable": "y",
    "kernelTypeNullable": "notebook", "languageNullable": "r",
})
assert already_ok["kernelTypeNullable"] == "notebook"
assert already_ok["languageNullable"] == "r"
print("already-correct call preserved: OK ->", already_ok)

# Omitted kernelType/language default to the safe, common combination.
defaulted = _fix_save_notebook({"newTitleNullable": "x"})
assert defaulted["kernelTypeNullable"] == "script"
assert defaulted["languageNullable"] == "python"
print("omitted fields defaulted: OK ->", defaulted)

# None values are dropped, not forwarded as literal nulls.
no_nulls = _nullable_suffixed({"newTitle": "x", "text": None})
assert "textNullable" not in no_nulls
print("None values dropped: OK ->", no_nulls)


# The upstream `npx mcp-remote` child must inherit proxy/CA settings: the mcp
# SDK's default child env drops them, which behind a TLS-intercepting proxy
# made npm fail with SELF_SIGNED_CERT_IN_CHAIN (see docs/debug_notes.md).
import os
from ds_agent.kaggle_mcp import _upstream_env

_saved = {k: os.environ.get(k) for k in ("HTTPS_PROXY", "NODE_EXTRA_CA_CERTS", "KAGGLE_MCP_TOKEN")}
os.environ.update({"HTTPS_PROXY": "http://proxy:3128", "NODE_EXTRA_CA_CERTS": "/ca.crt",
                   "KAGGLE_MCP_TOKEN": "secret"})
try:
    env = _upstream_env()
    assert env["HTTPS_PROXY"] == "http://proxy:3128", env
    assert env["NODE_EXTRA_CA_CERTS"] == "/ca.crt", env
    assert "PATH" in env, "must still include the SDK's safe defaults"
    assert "KAGGLE_MCP_TOKEN" not in env, "only allow-listed vars are passed through"
    print("upstream npx env carries proxy/CA vars (and nothing else extra): OK")
finally:
    for k, v in _saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print("\nALL KAGGLE PROXY CHECKS PASSED")
