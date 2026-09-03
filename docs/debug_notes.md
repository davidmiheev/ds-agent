# Debug notes

Hard-won lessons from getting this stack working. Consult before re-debugging
the same symptoms.

## BYOK / provider env

- **`ANTHROPIC_API_KEY` must be set to the empty string `""` — not unset —**
  when redirecting the claude CLI to a non-Anthropic base URL (OpenRouter,
  gateways). If it's merely absent, the CLI silently falls back to
  first-party Anthropic auth and every request 401s. See `providers.py`.
- OpenRouter needs `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` for the
  live model catalog to work.

## MCP wiring

- **The claude CLI spawns MCP subprocesses with `cwd` = the per-session
  workspace, not the project root.** So `python -m colab_mcp.colab_server`
  fails with ModuleNotFoundError unless the mcp.json env block sets
  `PYTHONPATH` to the project's `src/` dir. This bit us once; every custom
  MCP entry in mcp.json needs it.
- **Colab proxy mode doesn't work headless.** The original colab-mcp
  `open_colab_browser_connection` tool returned `{"result": false}` in the
  wiring test (`tests/colab_mcp_wiring_test.txt`) — it needs a real browser
  session. That's why `src/colab_mcp/colab_server.py` was written as a
  programmatic wrapper around `googlecolab/google-colab-cli` instead.
- **Colab OAuth**: reuse the public OAuth client shipped inside
  google-colab-cli (client id `764086051850-...`). No need to register a GCP
  OAuth client; tokens cache at `~/.config/colab-cli/token.json`.
- **Colab deps need their own venv** (`bash src/colab_mcp/setup.sh` →
  `src/colab_mcp/.venv`, Python 3.13). google-colab-cli's dependency set
  conflicts with the main app venv. Note: the repo's `.venv-313/` at the
  project root is an **empty husk** (only pyvenv.cfg) — don't point anything
  at it; `tests/test_colab_mcp_server.py` falls back to `src/colab_mcp/.venv`.
- **SSL on sandboxed hosts**: behind a corporate proxy, MCP HTTP calls fail
  cert verification. Fix by setting `REQUESTS_CA_BUNDLE` (and/or
  `CURL_CA_BUNDLE`) in the mcp.json env block to the gateway CA path, e.g.
  `/etc/ssl/certs/agent-identity/sandbox-gateway-ca.crt`. `research_mcp`
  reads these explicitly (`server.py` `_ssl_ctx()`). **Caveat: the CA file
  must actually exist.** Pointing at a missing file makes every requests
  call raise `OSError: Could not find a suitable TLS CA certificate bundle`
  — which (for colab) cascaded into the auth hang below. On this host the
  file doesn't exist, so the entries were removed (2026-08-26).
- **Never call `input()` in an MCP stdio server.** The subprocess's stdin is
  the MCP JSON-RPC pipe, so a blocking `input()` (e.g. colab_cli's
  `_run_remote_flow` OAuth prompt) hangs the tool call until the client's
  timeout (symptom: `tool "colab_sessions" timed out after 120s`).
  `colab_server._get_creds()` therefore loads `token.json` directly and
  refreshes it non-interactively; interactive auth is done once via
  `src/colab_mcp/auth_once.py`.
- **colab_cli's `Client(env, session)` wants a session, not Credentials.**
  Passing raw `google.oauth2.credentials.Credentials` gives
  `'Credentials' object has no attribute 'request'` on the first API call
  (tools that don't hit the network, like `colab_status`, still "work").
  Wrap with `google.auth.transport.requests.AuthorizedSession(creds)`,
  exactly like `colab_cli.auth.get_credentials()` does.
- `WaitForMcpServers` reports `ready: false` on the first call right after
  session start — normal; the agent should call it again. In transcripts,
  `filesystem` (npx cold start) is often "still connecting" while `colab` /
  `research` are already up.

## SDK message serialization (`sessions.py::_serialize`)

- `ResultMessage` has **no `type` field** — inject it from the class name.
- Content blocks (`TextBlock`, `ToolUseBlock`, `ThinkingBlock`,
  `ToolResultBlock`) are **dataclasses, not Pydantic models** — no
  `model_dump()`. Use `dataclasses.asdict` recursively and inject a `type`
  discriminator so the browser can switch on block kind.
- Cost/usage: sum `ResultMessage.model_usage` per-model entries; the
  top-level `total_cost_usd` sometimes differs — take `max()` of both.

## Artifacts & trimming

- Artifact markers (`__ARTIFACT__:kind:/path`) are extracted from tool
  result text **after** trimming — the parser looks at head/tail lines, so
  it still works on trimmed output. Keep that order.
- Trimmed outputs land in `<workspace>/.truncated/<tool>-<hash>.txt`; the
  model sees head + `[truncated]` + tail + a pointer to the full file.

## Stuck agent incident (2026-08-26) — root cause & fix

**Symptom**: user sent "can you run your both of your code blocks on kaggle?"
and the UI showed `assistant — working…` forever. The stop button did nothing
useful; the session was unusable until a page refresh.

**What was actually stuck**: NOT an MCP tool call. The transcript shows the
user message was written at 21:41:37 and **no assistant response ever
followed** — the hang was on the **model API call itself**. The claude CLI
held an ESTABLISHED TCP connection to OpenRouter (`google/gemma-4-31b-it`)
that never returned a single byte. The server log even flagged
`[claude-code:unrecognized_model] {"model":"google/gemma-4-31b-it"}` right
before the hang — the CLI didn't recognize the model and the upstream request
sat open indefinitely.

**Why nothing recovered it**:
1. `stream_events()` did a bare `async for msg in receive_messages()` with
   **no timeout anywhere** — a hung model call blocks the generator forever.
2. The UI's `busy` flag only clears on a `result` frame, which never came,
   so "working…" stayed up.
3. When the user finally hit stop, the interrupt killed the claude subprocess,
   but the in-memory `ActiveSession` kept holding the **dead client** — the
   next message would write into a dead pipe.

**Fix** (sessions.py + app.js):
- `stream_events()` is now long-lived (survives across turns; the SDK stream
  stays open) and reads messages through a queue with
  `asyncio.wait_for(timeout=TURN_INACTIVITY_TIMEOUT)` (default 300s, env
  `TURN_INACTIVITY_TIMEOUT`). The watchdog is only armed while
  `active.turn_active` is set (set by the steer pump on query, cleared on the
  result frame), so idle time between turns never false-positives.
- On timeout: emit a `system/watchdog` frame → `interrupt()` → wait
  `TURN_RECOVERY_TIMEOUT` (default 30s) for a result. If the CLI died
  (`client_alive()` checks `transport._process.returncode`), `respawn()`
  disconnects and spawns a fresh client resuming the transcript.
- UI handles `error` / `reader_error` / `system.watchdog` frames by clearing
  `busy` and showing a ⚠ line, so the UI can never get stuck on "working…".
- `app.py` cancels the reader task on WS disconnect (stream_events no longer
  self-terminates on result).

**Lesson**: any `async for` over an external stream needs an inactivity
timeout, and "the model call hung" is a real failure mode for gateway
providers (OpenRouter) — it is NOT always an MCP tool that's stuck. Check the
transcript: if the user message is the last entry with no assistant reply,
the hang is on the model API, not a tool.

## End-to-end test status (2026-08-23/24)

- `tests/colab_mcp_end_to_end_test.txt` — PASS: `colab_status` ("No active
  session"), `colab_auth` returns auth URL, stdio smoke lists 7 tools.
- `tests/research_mcp_end_to_end_test.txt` — `pubmed_search` returned real
  results, but the run ended in **TIMEOUT** waiting for the final `result`
  frame (the agent kept going past the 85s recv deadline). Not a server bug;
  raise the deadline or make the prompt stricter.
- `tests/colab_mcp_wiring_test.txt` — historical record of the failed
  browser-proxy approach (see MCP wiring above).
- All WS tests hardcode `http://127.0.0.1:8765` — start the server with
  `bash scripts/run_server.sh` first, and make sure an OpenRouter key is
  stored (Settings → BYOK) or session creation 400s with "no key stored".

## History / transcript

- **Transcript location**: the SDK does NOT write to
  `~/.coding-agent/sessions/<sid>/transcript.jsonl` (that path in
  `_has_transcript` is legacy — the dir is always empty). Real transcripts
  live at `~/.claude/projects/<slug>/<uuid>.jsonl` where slug = the session
  workspace path with **every non-alphanumeric char replaced by `-`**
  (so `~/.coding-agent/workspaces/abc` → `-home-david--coding-agent-workspaces-abc`
  — note the double dash from `/.`). One file per CLI process; newest mtime
  = current conversation.
- Transcript entries: `user` (string content = real user message; list
  content = tool results), `assistant` (content blocks: text/thinking/
  tool_use), plus noise types (`queue-operation`, `attachment`,
  `last-prompt`) that must be skipped. There are **no `result` entries** on
  disk — per-turn usage only exists in the DB (`session_usage`).
- `load_history` runs in a thread (`asyncio.to_thread`) — transcripts can
  be multi-MB.
- **Resume bug (fixed)**: `open()` passed our sid as `resume=`, but the SDK
  expects *its own* session UUID (the transcript filename). Also
  `_has_transcript` checked a legacy path that never exists, so resume was
  silently never set. Now `resume=_sdk_session_id(workspace)` = newest
  transcript's stem.

## Concurrent web UI + Telegram on the same session: races, misrouted replies, spurious interrupts (2026-09-03)

**Symptom**: with a web UI tab open on a session, sending a Telegram message
to that same session got "strangely interrupted", and a follow-up message
got no response at all — a recurrence of the bug below, on the same session
(`c0b22fad52fa452b`), even after the "always notify" telegram.py fix landed.

**Root cause**: `stream_events(active)` was called independently by every
consumer — once per WebSocket connection in `app.py`, and freshly on *every
single Telegram message* in `telegram.py`. Each call spawned its own
`_pump()` task doing `async for msg in active.client.receive_messages()`.
When a web UI tab and a Telegram turn were both alive on the same
`ActiveSession`, **two tasks iterated the same SDK message stream
concurrently** — an unsupported, racy pattern. Whichever task happened to
be scheduled to receive a given message got it; the other consumer never
saw it. Concretely:
- If Telegram's reply landed in the web UI's private queue instead of
  Telegram's, Telegram's own watchdog (armed because `turn_active` is
  true but *its* queue got nothing) fired after `TURN_INACTIVITY_TIMEOUT`
  and called `interrupt()` — killing the turn the web UI was actually
  receiving fine. That's the "strangely interrupted" symptom.
- After such an interrupt, if the CLI process died, `respawn()` swapped in
  a fresh client but the original queued message was never resent — silently
  dropped. Next message to the session: still nothing, because the reply to
  *that* one could just as easily be won by the other consumer's queue.

**Fix** (`sessions.py`): replaced the per-caller reader with a single shared
engine per `ActiveSession`, `_run_turn_engine()`, started lazily and exactly
once (`_ensure_engine_started`). It owns the one `_pump()` reading
`active.client.receive_messages()`, the one `_steer_pump()` sender, and the
watchdog/interrupt/respawn logic — and fans out every frame
(`_broadcast()`) to all currently-registered subscriber queues. Callers no
longer read the SDK stream directly; they call `subscribe()` to register a
queue and `stream_from()`/`stream_events()` to consume it. `app.py` and
`telegram.py` were switched to this. A useful side effect: a web UI tab and
a Telegram chat on the same session now see the *same* live stream, instead
of racing for it.

**Second, subtler race this exposed**: `stream_events()`/`subscribe()`
registers a subscriber queue — but `stream_events()` is a lazy async
generator, so *nothing in its body runs* (including registering the queue)
until it's first iterated. If the shared engine is already running (e.g. a
web UI tab already has the session open) and a caller calls
`send_user_message()` before it starts iterating `stream_events()`, the
already-running `_steer_pump` can dispatch and finish that turn before the
caller's subscription exists — the caller then waits forever for a reply it
already missed. Fixed by splitting `subscribe()` out as a **synchronous**
call (registers immediately, no lazy generator involved) that both
`app.py::ws_session` and `telegram.py::_run_agent_turn_for_telegram` now
call *before* `send_user_message()` / before their receive loop can call it.

**Known remaining limitation**: if two *genuinely overlapping* turns happen
to be in flight from different interfaces at nearly the same moment, a
freshly-subscribing consumer can still observe the tail of the *other*
interface's already-in-flight turn (including its `result` frame) before
its own turn is dispatched, and misattribute that result as its own reply.
Turns are already serialized session-wide (`turn_done`, see the entry
below), so this window is narrow, but fully closing it needs per-turn
correlation (tagging each result with which enqueued message produced it),
which is a bigger change than this fix. Not yet done.

## Telegram: turn completes silently, no reply ever arrives (2026-09-03)

**Symptom**: user reported sending a message into session `c0b22fad52fa452b`
("Telegram Session 11") via Telegram and getting no response at all — not
even an error.

**Root cause chain**:
1. That session's model was `openrouter` / `google/gemma-4-31b-it` — the
   exact same id from the "Stuck agent incident (2026-08-26)" above.
   **Correction**: an earlier version of this note called this id
   "nonexistent"/"hallucinated" — that was wrong. Checked directly against
   OpenRouter's live `/api/v1/models` catalog (2026-09-03): it's real
   (`google/gemma-4-31b-it`, plus `:free`/`:batch` variants). The actual
   problem is that the *bundled Claude Code CLI* (pinned via
   `claude-agent-sdk>=0.2.144`, CLI `2.1.251` on this host) has its own
   internal model-recognition list that predates Gemma 4 and doesn't know
   the id — it logs `[claude-code:unrecognized_model]` and then, rather than
   failing fast or passing the request through as-is, the call to
   OpenRouter just hangs with no bytes ever coming back. This is a
   CLI/SDK-version limitation, not a bad model choice — `${CLAUDE_CODE_ENABLE_
   GATEWAY_MODEL_DISCOVERY}=1` (already set for openrouter, see
   `providers.py`) does not prevent it.
2. The session's `.claude/projects/.../` dir was created (CLI spawned) but
   never got a transcript `.jsonl` — consistent with the very first query
   hanging before anything was ever written, exactly like the original
   incident.
3. The stuck-agent watchdog (`sessions.stream_events`,
   `TURN_INACTIVITY_TIMEOUT=300s`) exists precisely to recover from this —
   but recovery (interrupt, or interrupt+respawn) produces a **`result`
   frame with no assistant text**. That surfaces fine in the web UI (which
   shows the turn ended), but `telegram.py::_run_agent_turn_for_telegram`'s
   `result` handler only calls `send_message` **if `final_text` is
   non-empty** and only sends artifacts if any `.png` appeared — if neither,
   it silently `return`s. So a turn that hangs, gets recovered by the
   watchdog, and comes back empty produces **zero** Telegram output — from
   the user's side this is indistinguishable from the bot being dead.
4. Separately, journalctl shows this host has frequent transient breakage
   talking to `api.telegram.org` (`Connection reset by peer`, `SSL
   handshake operation timed out`, roughly every 10-40 min). `TelegramAPI`
   catches all of that in `_request` and just returns `{"ok": False}` — no
   retry. A single blip on the one `send_message` call meant to report a
   turn error/timeout means that message is lost forever too.

**Fix** (`telegram.py`):
- `_run_agent_turn_for_telegram`'s `result` handler now sends an explicit
  "turn finished without producing a reply" fallback message when there's
  no text and no artifact — the user always gets *something*, even for a
  botched/interrupted turn.
- `TelegramAPI._request`/`call` gained a `retries` param (default 1 retry,
  1.5s apart) so a transient network blip on `sendMessage`/`sendPhoto`
  doesn't silently eat the message. `getUpdates` explicitly passes
  `retries=0` — it's a 30s long-poll already retried by the outer
  `run_bot_polling` loop, so stacking a blocking retry would just double
  the delay before that loop notices and tries again.

**Not fixed here (still relevant)**: `google/gemma-4-31b-it` (and presumably
any other model newer than the bundled CLI's recognition list) will hang
again if reused — validating against OpenRouter's *live* catalog won't
catch this, since the id is genuinely valid there. What would actually help:
(a) upgrading `claude-agent-sdk`/the bundled CLI so its model list includes
newer releases, and/or (b) a session-creation-time smoke-test query with a
short timeout (e.g. a cheap 1-token request) so an id the CLI can't talk to
is caught and reported in seconds instead of silently hanging the first
real turn for `TURN_INACTIVITY_TIMEOUT` (5 min).

## Telegram `/models` search only matched the first word (2026-09-03)

**Symptom**: `/models gemini 3.7` behaved like `/models gemini` — the
version qualifier was silently dropped.

**Root cause**: the handler took `query = parts[1]` from
`text.strip().split()` — i.e. only the second whitespace-separated token,
discarding everything after it. It was also a single case-sensitive-safe
but punctuation-strict substring check (`query.lower() in id.lower()`), so
even a correctly-captured `"gemini 3.7"` wouldn't match an id like
`google/gemini-3.7-flash` (no literal `"gemini 3.7"` substring — the id
uses a hyphen, not a space).

**Fix**: `query = " ".join(parts[1:])` to capture the full remainder, plus
a new `_model_matches_query()` that normalizes both the query and the
id+label haystack (lowercase, punctuation → spaces) and requires every
query token to appear in the haystack (order-independent, AND-matched).
`"gemini 3.7"`, `"GEMINI 3-7"`, and `"gemini3.7"` all now match
`google/gemini-3.7-flash`.

## Git / network

- **SSH to GitHub fails over IPv6** on this box: `git push` dies with
  `Connection closed by 64:ff9b::8c52:7903 port 22` (the IPv6 route is
  black-holed). Fix: force IPv4 —
  `git -c core.sshCommand="ssh -4" push -u github main`.
- The dead SOCKS proxy (`ALL_PROXY=socks5://127.0.0.1:1080`) also breaks
  plain `curl` — use `curl --noproxy '*'`.

## Server startup gotchas

- `app.py` resolves `static/` and `templates/` relative to `__file__`
  (`HERE = Path(__file__).parent`), so they must stay inside the
  `ds_agent` package dir — they were moved there in the restructure.
- Run via module path now: `uvicorn ds_agent.app:app` (with `src/` on
  `PYTHONPATH` or the package installed), not `app:app`.
- Empty `APP_PASSWORD` = auth fully bypassed (localhost mode). `db.check_cookie`
  returns True for any token in that case — intentional.
