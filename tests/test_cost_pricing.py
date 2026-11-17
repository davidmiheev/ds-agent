"""Session cost accounting: catalog pricing (incl. $0 free models) must win
over the SDK's Anthropic-priced total_cost_usd estimate."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ds_agent import sessions


def _msg(usage, total_usd):
    return SimpleNamespace(model_usage=usage, total_cost_usd=total_usd,
                           num_turns=1, duration_ms=1)


def _record(msg, pricing):
    captured = {}
    with patch.object(sessions.db, "record_usage", lambda sid, u: captured.update(u)), \
         patch("ds_agent.model_catalog.get_model_pricing", side_effect=lambda m: pricing.get(m)):
        sessions._record_result_usage("sid", msg)
    return captured


# a free model: catalog knows it and prices it at 0 — the SDK's $0.5 estimate
# must NOT leak through the zero-cost fallback
u = {"nvidia/x:free": {"inputTokens": 1000, "outputTokens": 500, "costUSD": 0.5}}
out = _record(_msg(u, 0.5), {"nvidia/x:free": {"prompt": "0", "completion": "0"}})
assert out["cost_usd"] == 0.0, out
print("free model records $0 despite SDK costUSD estimate: OK")

# a paid model: prompt/completion rates applied
out = _record(_msg(u, 0.5), {"nvidia/x:free": {"prompt": "0.000001", "completion": "0.000002"}})
assert abs(out["cost_usd"] - (1000 * 1e-6 + 500 * 2e-6)) < 1e-9, out
print("paid model uses catalog rates: OK ->", out["cost_usd"])

# a model missing from the catalog falls back to the SDK numbers
out = _record(_msg(u, 0.5), {})
assert out["cost_usd"] == 0.5, out
print("uncatalogued model falls back to costUSD: OK ->", out["cost_usd"])

# cache tokens use the catalog's cache rates when present
u2 = {"m": {"inputTokens": 0, "outputTokens": 0, "costUSD": 0,
            "cacheReadInputTokens": 1000, "cacheCreationInputTokens": 100}}
out = _record(_msg(u2, 0.9),
              {"m": {"prompt": "0.000001", "completion": "0.000001",
                     "input_cache_read": "0.0000001", "input_cache_write": "0.000002"}})
assert abs(out["cost_usd"] - (1000 * 1e-7 + 100 * 2e-6)) < 1e-9, out
print("cache-token rates honored: OK ->", out["cost_usd"])

print("ALL COST PRICING CHECKS PASSED")
