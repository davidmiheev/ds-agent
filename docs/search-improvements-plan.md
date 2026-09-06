# Plan: Advanced Techniques for Cross-Session Search

> Status: proposal, not implemented. Current state (`src/ds_agent/search.py`,
> shipped in PR #2) is a plain substring scan. This doc plans what comes
> after, if/when it's actually needed — see Recommendation at the bottom.

## 1. Current state and its limits

`search_sessions()` (`search.py`), exposed to the agent as the
`mcp__memory__search_other_sessions` tool, does exactly what its own
docstring says: *"a plain substring scan over each session's latest
transcript + workspace file listing — no index to maintain."*

- **Query handling**: lowercase, strip, then `text.lower().find(q)` —
  one literal substring check, no tokenization.
- **Scope per call**: every session, newest-first, each one fully
  re-parsed from its on-disk JSONL transcript (`sessions.load_history`)
  on every single search — no cached/indexed representation.
- **Ranking**: none. Results are in whatever order `db.list_sessions()`
  returns (recency), not relevance; a session with one weak match ranks
  the same as one with five strong ones.
- **Matching gaps this causes**: word reordering ("trading strategy" vs.
  "strategy for trading"), synonyms, typos, and multi-word queries that
  aren't a single contiguous phrase in the source text all fail silently
  — the tool returns an empty result with no signal that the *phrasing*,
  not the *content*, was the problem.
- **Scaling**: fine for a handful of sessions (this is a single-user
  personal tool); becomes a real linear-scan cost once there are hundreds
  of sessions with long transcripts, since every call re-reads and
  re-parses every transcript file from scratch.

This mirrors the same "flat, no-index, substring-only" starting point as
the `memories` table before `docs/memory-graph-plan.md` — worth reading
together, since Tier 4 below and that doc's §6 "no semantic retrieval"
open question are the same underlying gap.

## 2. Constraints (don't over-build this)

- **Single-user personal tool.** No new *service* infrastructure —
  everything below either ships in Python's stdlib `sqlite3` module
  already, or is a small importable library. No Elasticsearch, no
  standalone vector DB.
- **Stay in SQLite.** `state.db` is already the source of truth for
  sessions/usage/memories; search should live there too, not a second
  datastore.
- **Ship incrementally.** Each tier below must be independently useful
  and non-breaking on its own — no big-bang rewrite of `search.py`.
- **Precedent already in this codebase**: `telegram.py`'s `/models`
  command hit the exact same class of bug (`/models gemini 3.7` only
  matched `gemini`, and even a correctly-captured multi-word query failed
  because id/label use hyphens, not spaces) and was fixed with
  `_normalize_model_query()` + `_model_matches_query()` — punctuation
  normalization plus token-level AND-matching, no new dependency. Tier 1
  below is that same fix, generalized to session search.

## 3. Proposed tiers

### Tier 1 — tokenized, normalized matching (cheap, no new infra)

Replace the single `text.lower().find(q)` with the same pattern already
proven in `telegram.py`: normalize punctuation to spaces, split the query
into tokens, require every token to appear (order-independent) in the
normalized haystack. Handles multi-word queries regardless of exact
phrasing/punctuation, and stays a pure in-memory string operation — no
schema change, no new dependency, still O(n) over all sessions per call.

### Tier 2 — SQLite FTS5 full-text index (still no new infra)

FTS5 ships built into the `sqlite3` module Python already links against
(`CREATE VIRTUAL TABLE messages_fts USING fts5(session_id, role, text)`).
This is the single highest-leverage change:

- **Real tokenization + stemming** (the `porter` tokenizer folds
  "trading"/"trade"/"traded" together), **prefix search** (`trad*`),
  and **phrase queries** (`"exact phrase"`) for free.
- **Relevance ranking** via FTS5's built-in `bm25()` — sessions with
  stronger/denser matches surface first instead of arbitrary recency
  order.
- **Actually scales**: an indexed lookup instead of re-reading and
  re-parsing every session's transcript file on every call.

Cost: messages currently exist only as JSONL transcript files on disk,
re-parsed on demand — there's no table of message rows to index yet. This
tier requires **materializing messages into SQLite** (a `messages` table,
populated incrementally — e.g. append on each turn's `result` frame in
`sessions.py`, or lazily on first search with a `transcript_synced_at`
watermark compared against the transcript file's mtime) with an FTS5
virtual table (or FTS5 `content=` shadow table) kept in sync via triggers
or explicit inserts. This is the real work in this tier; the querying
side is a few lines.

### Tier 3 — fuzzy matching for typos (optional, cheap)

Layer `difflib.SequenceMatcher` (stdlib, zero new dependency) or
`rapidfuzz` (small C-extension dependency, much faster) as a fallback:
if Tier 1/2 return nothing, retry with a fuzzy/near-match threshold
against session titles and top FTS5 candidates. Catches typos
("bactesting") that literal and stemmed matching both miss. Cheap to add
once Tier 2 exists (reduces the fuzzy-match search space to already-close
candidates instead of every session).

### Tier 4 — semantic (embedding) search (bigger lift, real cost)

Embed message/memory text via a small model (a local
`sentence-transformers` model, or an API — OpenAI/Voyage/whatever's
already BYOK-configured), store vectors via the `sqlite-vec` extension
(still SQLite — a loadable extension, not a new service), and do
cosine-similarity nearest-neighbor search. This is what catches
conceptually related but lexically different content — "the trading bot"
finding a session about "market-making strategy" with zero shared
vocabulary, which no amount of stemming or fuzzy-matching will ever catch.

This is explicitly the most speculative tier, same as
`memory-graph-plan.md` §6 flags it: real embedding-generation cost/latency
per remembered fact or per session close, a new dependency (local model
weights, or an API call + key), and meaningfully more moving parts than
everything above it.

### Tier 5 — hybrid keyword + rerank (only if Tier 4 ships)

Classic pattern once embeddings exist for another reason anyway: use
FTS5/BM25 for fast keyword recall (candidate generation), then rerank the
top-K candidates by embedding similarity. Best precision of any tier here,
but strictly dominated by "don't build Tier 4 speculatively" — this tier
only makes sense as a follow-on, never as a starting point.

## 4. Migration plan (phased, each phase independently shippable)

| Phase | Change | Breaking? |
|---|---|---|
| 0 (done, PR #2) | Plain substring scan (`search.py`). | — |
| 1 | Tokenized/normalized AND-match, generalizing `telegram.py`'s `_model_matches_query` pattern into `search.py`. No schema change. | No — same function signature, same result shape, just better matching. |
| 2a | Materialize messages into a SQLite table, populated incrementally as turns complete. | No — additive table; `search.py` keeps working unchanged until 2b switches it over. |
| 2b | FTS5 virtual table over the messages table; `search_sessions()` queries it instead of re-parsing transcripts, ranked by `bm25()`. | Low — same tool signature/result shape; result *ordering* changes from recency to relevance, which is the intended improvement but is a behavior change worth calling out to the user. |
| 3 | Fuzzy fallback (`difflib`/`rapidfuzz`) when Tier 1/2 return nothing. | No — additive, only fires on empty results. |
| 4 (speculative) | Embeddings + `sqlite-vec` nearest-neighbor search. | No, if added as a new `search_mode="semantic"` option alongside the existing keyword path rather than replacing it. |
| 5 (speculative, depends on 4) | Hybrid BM25 recall + embedding rerank. | No, if layered as a ranking step after Tier 2b's candidate list. |

## 5. Open questions / risks

- **Index staleness.** Tier 2 needs messages synced into SQLite as turns
  happen (or a mtime-watermark lazy-sync check) — an out-of-sync index
  that misses recent turns is worse than the current always-fresh
  file-parse approach in one specific way (silently stale results look
  identical to "no results"), so the sync mechanism needs to fail loud,
  not silent, if it ever gets confused.
- **Embedding cost/latency is real money and real time** (Tier 4) — needs
  a concrete trigger (actual evidence FTS5 keyword search misses things
  users care about) before committing to it, not speculative "might be
  nice."
- **Complexity vs. actual benefit**, same caveat as
  `memory-graph-plan.md` §6: as of this writing there's no evidence the
  current substring scan has actually failed a real query — Tier 1 exists
  because the *identical* bug class was already observed and fixed in
  `/models`, so it's a reasonable preemptive fix, not proven-needed. Tiers
  2+ should wait for the session/message volume where "the search
  literally couldn't find something I know is there" becomes a real,
  observed complaint.

## 6. Recommendation

Ship **Tier 1 now** — it directly reuses a pattern already validated and
merged elsewhere in this codebase (`telegram.py`), costs nothing in new
dependencies or schema, and closes an already-demonstrated class of bug.
Hold Tier 2 (FTS5) until either session count/transcript volume makes the
full re-parse-every-call cost actually noticeable, or literal+tokenized
matching visibly fails to find something a user knows is there — at that
point Tier 2 is a clear, contained, high-value change. Treat Tiers 3-5 as
explicitly speculative, gated on real usage evidence, same posture as
`memory-graph-plan.md` takes on its own later phases.
