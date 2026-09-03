"""Unit-style regression test for search.py / export.py / the memories table.

No live server or SDK client needed — builds fake SDK transcripts directly
on disk and exercises the modules against a temp CODING_AGENT_HOME. Also
guards the one thing that must never happen: a session's resolved secrets
(.mcp.json / .claude/settings.local.json) leaking into a search result or
an export zip. Run with: PYTHONPATH=src python tests/test_search_export_memory.py
"""
import os, sys, tempfile, json
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ["CODING_AGENT_HOME"] = tmp
os.environ["APP_PASSWORD"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ds_agent import db, sessions, search as search_mod, export as export_mod, agent_prompt

db.init()

# --- build two fake sessions with fake transcripts (no real SDK/CLI spawn needed) ---
def make_session(title, msgs):
    row = sessions.create(provider="openrouter", model="anthropic/claude-sonnet-4.5", title=title)
    sid = row["id"]
    workspace = Path(row["workspace"])
    workspace.mkdir(parents=True, exist_ok=True)
    # fake a plot artifact in the workspace
    plot = workspace / "plot.png"
    plot.write_bytes(b"\x89PNG\r\n fakepngdata")
    # secrets that must NEVER be exported/searched into leaking
    (workspace / ".mcp.json").write_text(json.dumps({"mcpServers": {"x": {"env": {"KEY": "supersecret123"}}}}))
    (workspace / ".claude").mkdir(exist_ok=True)
    (workspace / ".claude" / "settings.local.json").write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "sk-secret-xyz"}}))

    # fake SDK transcript at ~/.claude/projects/<slug>/<uuid>.jsonl
    import re
    slug = re.sub(r"[^a-zA-Z0-9]", "-", str(workspace))
    proj = Path.home() / ".claude" / "projects" / slug
    proj.mkdir(parents=True, exist_ok=True)
    tp = proj / "fake-uuid-0001.jsonl"
    lines = []
    for role, text in msgs:
        if role == "user":
            lines.append(json.dumps({"type": "user", "message": {"content": text}}))
        else:
            lines.append(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}))
    tp.write_text("\n".join(lines) + "\n")
    return sid, workspace

sid1, ws1 = make_session("Bitcoin backtest", [
    ("user", "Can you backtest a moving-average crossover strategy on BTC-USD?"),
    ("assistant", "Sure — I trained a vectorbt strategy on BTC-USD daily data, Sharpe ratio was 1.4.\n__ARTIFACT__:plot:/nonexistent/plot.png"),
])
sid2, ws2 = make_session("Titanic EDA", [
    ("user", "Explore the titanic dataset for me"),
    ("assistant", "Loaded titanic.csv — 891 rows, survival rate 38%."),
])

print("sessions created:", sid1, sid2)

# --- search_sessions ---
results = search_mod.search_sessions("BTC-USD")
assert len(results) == 1 and results[0]["id"] == sid1, results
assert results[0]["matches"], "expected a message match"
print("search_sessions text match: OK")

results2 = search_mod.search_sessions("plot.png")
assert any(r["id"] == sid1 for r in results2), results2
found = [r for r in results2 if r["id"] == sid1][0]
assert "plot.png" in found["artifact_matches"], found
print("search_sessions artifact filename match: OK")

# secrets must never surface
results3 = search_mod.search_sessions("supersecret123")
assert results3 == [], f"LEAKED SECRET via search: {results3}"
results4 = search_mod.search_sessions(".mcp.json")
assert results4 == [] or all(".mcp.json" not in a for r in results4 for a in r["artifact_matches"]), results4
print("search_sessions never surfaces .mcp.json / secrets: OK")

# --- export ---
md, artifacts = export_mod.build_markdown(sid1)
assert "Bitcoin" in md or "backtest" in md.lower(), md[:200]
assert any(p.name == "plot.png" for p in artifacts), artifacts
print("export.build_markdown: OK, artifacts=", [p.name for p in artifacts])

zip_bytes = export_mod.build_zip_bytes(sid1)
assert len(zip_bytes) > 100
import zipfile, io
zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
names = zf.namelist()
assert "messages.md" in names
assert any(n.endswith("plot.png") for n in names), names
assert not any(".mcp.json" in n for n in names), f"LEAKED .mcp.json in export zip: {names}"
assert not any("settings.local.json" in n for n in names), f"LEAKED settings.local.json in export zip: {names}"
print("export.build_zip_bytes: OK, contains", names, "- no secrets leaked")

fname = export_mod.export_filename(sid1)
assert fname.endswith(".zip") and sid1[:8] in fname
print("export_filename:", fname)

# --- memory ---
mid1 = db.add_memory("User prefers vectorbt over backtesting.py for quant work", tags="preference", session_id=sid1)
mid2 = db.add_memory("Project uses OpenRouter as default provider", tags="convention")
mems = db.list_memories()
assert len(mems) == 2
print("db.add_memory / list_memories: OK ->", [m["text"] for m in mems])

filtered = db.list_memories("vectorbt")
assert len(filtered) == 1 and filtered[0]["id"] == mid1
print("db.list_memories query filter: OK")

ok = db.delete_memory(mid2)
assert ok
assert len(db.list_memories()) == 1
print("db.delete_memory: OK")

# system prompt injection
prompt = agent_prompt.build_append_system_prompt()
assert "vectorbt over backtesting.py" in prompt
assert "Persistent memory" in prompt
print("agent_prompt.build_append_system_prompt injects memories: OK")

db.delete_memory(mid1)
prompt2 = agent_prompt.build_append_system_prompt()
assert prompt2 == agent_prompt.DEFAULT_APPEND_SYSTEM_PROMPT
print("agent_prompt falls back to default when no memories: OK")

# --- sessions.get_active (bug fix) ---
assert sessions.get_active(sid1) is None  # no client spawned in this test
assert sessions.get_active("nonexistent") is None
print("sessions.get_active: OK")

print("\nALL SEARCH/EXPORT/MEMORY CHECKS PASSED")
