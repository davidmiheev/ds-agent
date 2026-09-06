# Debug notes

Hard-won lessons from getting this stack working. Consult before re-debugging
the same symptoms.

## Kaggle `save_notebook` fails with a generic error — field names need a `Nullable` suffix (2026-09-06)

**Symptom**: `mcp__kaggle__save_notebook({request: {isPrivate, newTitle, text}})`
always fails with `"An error occurred invoking 'save_notebook'."` — no
useful detail, `is_error: true`. Read/list kaggle tools (`list_models`,
`get_user_profile`) work fine, so it looked like an auth/permission issue
at first (that was the model's own hypothesis too, per its transcript
`thinking` block).

**Root cause**: reproduced directly against the real remote server
(`npx mcp-remote https://www.kaggle.com/mcp`, bypassing the CLI's generic
error wrapping) and inspected `save_notebook`'s actual JSON schema — every
field in the `request` object is suffixed `Nullable`:
`newTitleNullable`, `textNullable`, `isPrivateNullable`,
`languageNullable`, `kernelTypeNullable`, etc. Calling it with the
plain/guessable names (`newTitle`, `text`, `isPrivate`) doesn't error on
the field names themselves — they're just silently ignored as unrecognized
extra properties, leaving the actual title/content unset, which is what
produces the generic failure. This is Kaggle's own remote MCP server
design (not something in this repo to fix) — confirmed by calling it with
the correctly-named fields, which succeeded and created a real notebook:
`https://www.kaggle.com/code/davidmiheev/diag-test-notebook-correct-fields`.

**Fix**: since we don't control Kaggle's server, added explicit guidance
to `agent_prompt.py`'s Kaggle section naming the `Nullable`-suffix
convention directly, so the agent doesn't have to rediscover this by
trial and error every time.

**Caveat for next time**: reproducing this required actually calling
`save_notebook` against the live server, which creates a **real** (if
private) notebook on the account's Kaggle profile — there's no
`delete_notebook`/`delete_kernel` tool in the kaggle MCP tool list to clean
it up again via MCP. Prefer read-only kaggle tools for diagnosis where
possible; if a write tool must be tested live, flag it to the user before
running, not after.

**Follow-up same day: kaggle went from "erroring" to fully unavailable
mid-session.** Session `c0c150377c624ab1` later reported ALL kaggle tools
(including `list_models`, previously working) failing with
`No such tool available`, while `filesystem`/other MCP servers on the same
CLI process stayed fine. Checked `ps --ppid <cli-pid>`: the CLI's other MCP
children (filesystem, research, ds, memory) were all present; the kaggle
`npm exec mcp-remote` child was simply **gone** — it had exited at some
point during that CLI process's lifetime, and (like the `memory` server
earlier) a dead/never-reconnected MCP child stays dead for the rest of
that process's life; nothing auto-respawns just one child.

Considered and ruled out "my own diagnostic `mcp-remote` calls (same
bearer token) kicked this session's connection" — checked the *other* two
live sessions' kaggle bridges at the same time, both were still healthy
throughout (one older than the affected session's, one newer), so Kaggle
does not appear to enforce single-connection-per-token in a way that kills
existing connections. Cause of this specific child's exit is **not
confirmed** — could be a transient crash/network blip in that one
`mcp-remote`/node process instance. Not reproduced a second time.

**Fix**: same remedy as the memory-server case — kill the affected
session's CLI process (find it via `/proc/<pid>/cwd`, not `ps` argv) to
force `get_or_start()` to respawn it with fresh MCP children. One new
wrinkle worth noting: right after such a respawn, calling a kaggle tool
immediately can transiently return a much more informative error than the
generic "No such tool available" we saw with `memory`:
`"...still connecting. Call WaitForMcpServers to wait for it, then try
again."` — kaggle's `mcp-remote` bridge does a real OAuth-discovery +
remote-connect handshake (visible in its own stderr: "Discovering OAuth
server configuration... Connecting to remote server...") that takes a few
seconds, so this is expected and self-resolves; retrying a few seconds
later (or waiting ~15-20s post-respawn before the first kaggle call)
succeeds. Also noted: `WaitForMcpServers` appears to be usable only **once**
per session/transcript — every call after the first returned
`"WaitForMcpServers is disabled for this session, in subagents as well as
here"` even on a freshly respawned process; a short sleep-and-retry loop is
the practical workaround when it's already been spent.

**Follow-up #2 same day: STILL failing even with the `Nullable`-suffix fix
in the system prompt.** Reported again on the same session. Root cause was
two-fold:

1. **Conversational anchoring, same pattern as the memory-tools bug.**
   Read the transcript: the model's own `thinking` block for the failing
   attempt listed the plain field names (`newTitle`, `text`, `language`,
   `kernelType`, `isPrivate`) — copied near-verbatim from its OWN earlier
   failed attempts *earlier in the same `--resume`'d conversation* — with
   zero mention of the `Nullable` guidance that was genuinely present in
   its system prompt by then. It wasn't re-reading the prompt or the live
   tool schema; it was pattern-matching its own prior (wrong) tool calls.
   An explicit instruction ("ignore your prior attempts... check the exact
   real parameter names from the tool definitions you have access to
   right now") broke through immediately — the very next call correctly
   used the `Nullable`-suffixed names. **Lesson, generalized from both
   bugs**: once a model has made 2+ failed attempts at something *within
   one resumed conversation*, don't expect a system-prompt fix alone to
   change its next attempt — the model needs an explicit nudge to
   disregard its own established (wrong) pattern. This matters for judging
   whether a prompt fix "worked": test on a fresh, non-resumed session, not
   by asking an already-poisoned one to "try again."

2. **A second, independent bug, only visible once (1) stopped masking it**:
   even with the correct `Nullable` field names, the SAME generic
   `"An error occurred invoking 'save_notebook'"` still fired. This time
   the model had set `kernelTypeNullable: "python"` — invalid. Confirmed
   against Kaggle's own docs (`kaggle-cli`'s `kernels_metadata.md`):
   `kernelType` must be exactly `"script"` or `"notebook"` (never a
   language name); `language` must be exactly `"python"`, `"r"`, or
   `"rmarkdown"`. The model conflated the two fields — an easy mistake
   since the schema itself declares no `enum` for either
   `kernelTypeNullable` or `languageNullable` (unlike
   `kernelExecutionTypeNullable`, which does list its enum), so there's
   nothing in the live schema warning that `"python"` is invalid there.
   Verified the complete fix directly: `kernelTypeNullable: "script"` +
   `languageNullable: "python"` succeeded, creating
   `https://www.kaggle.com/code/davidmiheev/verify-kerneltype-fix`
   (a **third** real test notebook created during this investigation,
   after `diag-test-notebook-correct-fields` and one more from the
   anchoring-bias test — all left on the account; see the caveat above,
   still no delete tool available).

**Fix**: `agent_prompt.py`'s Kaggle guidance now includes a complete
known-working example call with real enum values, and explicitly calls out
that putting a language name into `kernelTypeNullable` produces the exact
same generic error as the field-naming bug, so a future
already-past-the-naming-fix failure doesn't get misdiagnosed as
auth/permissions again.

**Bonus, same investigation**: figured out how to check a saved notebook's
run results (the user asked, since none of this is discoverable from
`save_notebook`'s own response, which returns only `{ref, url,
version_number, kernel_id}`). `save_notebook` runs the notebook by default
— no separate "run" call needed. `get_notebook_session_status({request:
{userName, kernelSlug}})` gives run status (`COMPLETE`/etc.);
`list_notebook_session_output({request: {userName, kernelSlug}})` gives
the actual stdout/stderr log — verified against the real
`verify-kerneltype-fix` notebook (status `COMPLETE`, log correctly showed
`stdout: "1\n"` from its `print(1)`). Both use PLAIN `userName`/`kernelSlug`
fields, NOT the `Nullable` convention `save_notebook` uses — confirms the
general lesson: **every kaggle tool has its own independent field-naming
convention; never assume one tool's schema pattern applies to another,
check each tool's live schema individually.** `download_notebook_output`/
`download_notebook_output_zip` (for output files, not just the log) use
yet another field name for the same concept: `ownerSlug` instead of
`userName`.

**Follow-up #3 same day: reported failing YET AGAIN, concluded "lack of
write permissions."** Checked the transcript: the retry still used
`kernelTypeNullable: "python"` — completely unchanged despite the
system-prompt fix, because this specific `--resume`'d conversation has now
attempted `save_notebook` 8 times total, every single one with that exact
wrong value, and nothing had ever corrected it *within the conversation
itself*. Definitively disproved the permissions theory and closed the loop
in one message: told the session explicitly "kernelTypeNullable=python is
wrong, use script" — it immediately succeeded, and for good measure also
walked the full result-checking flow (`get_notebook_session_status` →
`COMPLETE`, `list_notebook_session_output` → real `stdout: "1\n"`),
creating a 4th real test notebook, `anchoring-test-fixed-2`.

**The generalized lesson, now confirmed a third time (memory tools,
kernelType, this)**: a system-prompt fix does not retroactively correct an
already-established wrong pattern in a long-running `--resume`'d
conversation — the model keeps repeating whatever it did before unless
something *in that specific conversation* explicitly overrides it. When
verifying whether a prompt fix "worked," always test on a fresh,
non-resumed session — testing by asking an already-poisoned session to
"try again" will very often still fail, not because the fix is wrong, but
because the conversation's own history outweighs the system prompt.

**Unrelated finding surfaced along the way**: the explicit-override message
sent just before the successful one hung for the full
`TURN_INACTIVITY_TIMEOUT` (300s) on `google/gemma-4-31b-it` and produced
garbled repeated-character output (`"sCous laL l' la l l l l l l ..."`)
before the watchdog's automatic interrupt recovered it (logged in the
transcript as `[Request interrupted by user]` — the SDK labels
watchdog-triggered interrupts the same as human-clicked ones, so don't
assume a user actually clicked stop just because the transcript says so).
**This means the gemma-4-31b-it hang from the "Stuck agent incident"
entry above is NOT fully fixed by the SDK upgrade — it's evidently just
less frequent now, not eliminated.** The watchdog + shared-engine +
Telegram-fallback-message mitigations (all from earlier entries in this
file) are exactly what's needed to survive this gracefully; nothing new
to fix here, just confirmation the underlying hang can still happen.

## Kaggle write-tool footguns fixed at the source: a local auto-correcting proxy (2026-09-06)

After three rounds of finding Kaggle `save_notebook` bugs (Nullable-suffix
field names, `kernelType`/`language` confusion, conversational anchoring
making prompt fixes unreliable — see the entries above), the durable fix:
`src/ds_agent/kaggle_mcp.py`, a local MCP server that sits between the CLI
and Kaggle's real remote MCP (`https://www.kaggle.com/mcp` via
`npx mcp-remote`), replacing the old direct `mcp-remote` entry in
`mcp.json`'s `"kaggle"` server.

**How it works**: uses the `mcp.server.lowlevel.Server` API (not
`MCPServer`/FastMCP — that one requires every tool to be a Python function
with a fixed signature, which can't dynamically proxy ~70 tools with
arbitrary nested schemas). `on_list_tools` connects to the real upstream
Kaggle MCP as a client and returns its tool list **verbatim** — nothing is
reimplemented, so all ~71 tools remain available and stay in sync with
whatever Kaggle changes on their end. `on_call_tool` forwards every call
unchanged EXCEPT `save_notebook`, whose `request` argument gets rewritten
before forwarding: plain field names become `Nullable`-suffixed, and
`kernelType` gets corrected to `"script"`/`"notebook"` if a language name
was put there instead (recovering that value into `language` if `language`
wasn't already set), defaulting to `script`/`python` if omitted entirely.

**A real implementation pitfall hit and fixed along the way**: the first
version connected to the upstream server lazily, on first request, from
inside the `on_call_tool`/`on_list_tools` handler. This crashed with
`RuntimeError: Attempted to exit a cancel scope that isn't the current
task's current cancel scope` — anyio's task-group-based cleanup (used
internally by `stdio_client`/`ClientSession`) requires a resource to be
entered and exited within the *same stable task* for its whole lifetime;
a per-request handler task is the wrong place, since its lifetime doesn't
match the connection's intended lifetime. Fixed by connecting to upstream
*before* starting the CLI-facing server loop, both nested in the same
top-level `_main()` coroutine.

**Verified end-to-end three ways**: (1) isolated stdio client test calling
the proxy directly with deliberately wrong plain-name arguments
(`kernelType: "python"`, no `Nullable` suffixes) — succeeded, created
`https://www.kaggle.com/code/davidmiheev/proxy-auto-fix-test`; (2) unit
tests for the correction logic itself with no network
(`tests/test_kaggle_mcp_proxy.py`); (3) through the **real CLI**, on the
exact session that had failed `save_notebook` 8 times in a row before this
fix, with a completely neutral prompt (no hints) — succeeded on the first
real attempt, `https://www.kaggle.com/code/davidmiheev/proxy-real-test`.
This is the first Kaggle notebook fix in this investigation that's robust
to conversational anchoring, since the correction happens after the model
decides what to call, not by hoping the model calls it correctly.

`agent_prompt.py`'s Kaggle guidance was simplified accordingly — the
detailed `Nullable`/enum-value explanation is no longer needed by the
model and was removed; only the (still-true, not auto-corrected)
notebook-result-checking guidance and the `ownerSlug`-vs-`userName`
inconsistency note remain.

**Caveat, same as every live test in this investigation**: this created
yet more real notebooks on the account — `proxy-auto-fix-test` and
`proxy-real-test`, in addition to `diag-test-notebook-correct-fields`,
`verify-kerneltype-fix`, and `anchoring-test-fixed-2` from earlier entries
(5 total now). Still no delete tool in Kaggle's MCP surface.

## Web UI double scrollbar + broken jump buttons — nested flexbox height chain (2026-09-06)

**Symptom chain** (three rounds of the same underlying bug class):
1. Added ⬆/⬇ jump-to-top/bottom buttons; user reported "arrows don't work
   and don't move with me." Root-caused via real headless-browser testing
   (Playwright — static reasoning about this kind of CSS is unreliable,
   don't skip straight to a real browser next time): `.chat`
   (a CSS Grid item, `display:flex; flex-direction:column`) had no
   `min-height: 0`. Grid/flex items default to `min-height: auto`, which
   lets them grow to fit content instead of respecting the grid row's
   height — so `#messages` never became an actual height-constrained
   scroll box; the whole page just grew instead
   (`#messages.clientHeight === #messages.scrollHeight`, confirmed
   ~20000px). Fixed by adding `min-height: 0` to `.chat` and `.messages`.
2. That exposed a second, genuinely obscure CSS fact: `bottom`/`right`
   offsets on an absolutely-positioned child *inside a container that
   itself scrolls* are resolved against the scrollable CONTENT's edge, not
   the visible viewport (`top`/`left` don't have this quirk — only
   `bottom`/`right` do). So the buttons, positioned directly inside
   `#messages`, scrolled away WITH the content instead of staying fixed in
   the corner. Fixed by moving them into a new non-scrolling sibling
   wrapper, `.messages-wrap`, so their containing block never scrolls.
3. User then reported "I have 2 scrollers." The `.messages-wrap` fix in
   (2) only handled `#messages` itself — `.sidebar`/`.files` (the OTHER
   two CSS Grid items in `.app`) had the exact same missing-`min-height:0`
   problem as `.chat` did in (1), so they could still grow taller than
   their grid row and push `.app` (which is `height: 100vh`) past the
   viewport — giving a page-level (document) scrollbar *in addition to*
   the now-correctly-contained `#messages` one. Confirmed via
   `document.documentElement.scrollHeight > clientHeight`. Fixed by adding
   `min-height: 0` to `.sidebar, .files` and to `.session-list` (a flex
   child inside `.sidebar` with the same pattern).
   **A `flex-direction: column` guess on `.messages-wrap` (reasoning that
   its default `row` direction made `#messages`'s `flex: 1` apply to the
   wrong axis) had ZERO measurable effect when tested — don't trust that
   kind of flex-axis reasoning without a real before/after measurement.**
   What actually fixed the leftover leak was blunt and reliable: explicit
   `overflow: hidden` on `.messages-wrap` itself. It doesn't need to
   scroll (only its `#messages` child does), so forcing it not to
   contribute its content height to its own ancestor's `scrollHeight`
   sidesteps whatever subtlety was making the flex-only approach leak.

**Lesson for next time this class of bug shows up**: any new `overflow:
auto` scroll region added to this app needs `min-height: 0` on *every*
flex/grid ancestor between it and the nearest explicitly-sized container
(here, `.app`'s `height: 100vh`) — missing it on even one intermediate
ancestor lets that ancestor grow to content size instead of clipping,
and the symptom (a second/wrong scrollbar, or scroll methods silently
doing nothing) shows up far from the actual missing property. Verify with
real browser measurements (`element.scrollHeight` vs `clientHeight`,
`document.documentElement` for the page level), not static CSS reading —
this took three iterations to fully find specifically because the first
two fixes were verified by reasoning/inspection rather than measurement,
and the actual behavior of nested flex/grid height chains (and of
`bottom`/`right` positioning inside scroll containers) is subtle enough
that intuition got it wrong twice in a row here.

## Pulling a new branch + restart is NOT enough to pick up a new MCP server (2026-09-06)

**Context**: reviewed/deployed the `session-search-export` branch (memory MCP
tools + cross-session search/export), which adds a new `memory` entry to the
repo's `mcp.json`. `git pull` + `systemctl restart coding-agent` alone did
**not** make it available to sessions.

**Why**: the repo's `mcp.json` is only a *template*. The file sessions
actually render `.mcp.json` from is `core.MCP_CONFIG_PATH` =
`~/.coding-agent/mcp.json` (i.e. `/home/agent/.coding-agent/mcp.json` on this
host) — a separate copy outside the git checkout. `scripts/deploy_remote.sh`
syncs the two (`install ... "$REMOTE_DIR/mcp.json" /home/agent/.coding-agent/mcp.json`,
step `[g]`), but that script runs from a *separate control machine* driving
the remote host over SSH — it's not something a plain `git pull` on the
target host itself invokes. Any change to the repo's `mcp.json` needs that
copy step repeated manually when deploying by pulling directly on the host
(as opposed to running the full `deploy_remote.sh` from elsewhere):

```bash
install -o agent -g agent -m 644 /opt/coding-agent/mcp.json /home/agent/.coding-agent/mcp.json
```

No service restart is required for this specific file — `_render_session_dir`
(sessions.py) reads `MCP_CONFIG_PATH` fresh on every session spawn, not once
at process startup. Restarting does matter for anything already warm in
`_active`, though: an already-running session's `.mcp.json` was rendered once
at its own spawn time, so it won't pick up a newly-added server until it
respawns (dead CLI, or a full service restart clearing the in-memory
`_active` registry).

**Lesson**: after pulling a branch that touches `mcp.json`/`mcp.json.example`,
always diff the repo copy against the live `~/.coding-agent/mcp.json` and
sync it — a plain restart will look clean (no errors) while silently missing
the new server, since a missing/extra MCP entry produces no startup error at
all, only an agent that doesn't have a tool it should.

**"Sending it a new message" does NOT force a respawn on its own** — this
tripped us up while verifying the fix. `get_or_start()` only respawns when
`client_alive()` is false, i.e. the underlying `claude` CLI process has
actually exited. A session that's just sitting there idle stays alive
indefinitely (`ClaudeSDKClient` is kept warm across turns on purpose — see
"Session Manager" in `docs/arch.md`), so a normal message to it reuses the
same already-connected process and its already-decided (stale) MCP tool set
forever, no matter how many new messages you send. Confirmed directly: a
session's CLI process was still running 20+ minutes after the config sync,
completely unaffected by a fresh message sent to it during that window.

**How to actually force it** for one specific stuck session, without
restarting the whole service (which affects every session):
```bash
# find the PID: match /proc/<pid>/cwd, NOT `ps` command text — the SDK
# passes cwd as an internal param, so the workspace path never appears in
# the process's argv/command line.
for pid in $(pgrep -f "claude_agent_sdk/_bundled/claude"); do
  readlink -f /proc/$pid/cwd
done
kill -TERM <pid>   # graceful; the app's crash-recovery path respawns it
                    # automatically on the next get_or_start() for that sid
```
After this, the *next* real touch (a message, or the web UI opening the
session — `ws_session()` calls `get_or_start()` before anything else) sees
a dead process and respawns fresh, re-rendering `.mcp.json` from the current
global config. A full `systemctl restart coding-agent` is the blunter
alternative when you don't need to preserve every other session's warm
state.

**Confirmed in the wild** (same day): session `c0c150377c624ab1` was spawned
at 18:45:21 UTC — 13s before the sync landed at 18:45:34 UTC — so its CLI
process's MCP tool registry was permanently frozen without `memory` (MCP
server connections are established once at CLI startup, never re-read).
Asked to "check you can search other sessions", the model correctly tried
`search_other_sessions(...)` and got back the CLI's own authoritative
`<tool_use_error>Error: No such tool available: search_other_sessions</tool_use_error>`
— proof this is a real missing-connection, not a model mistake. It then
found `ListAgents`/`SendMessage` (an unrelated built-in Claude Code CLI
feature for discovering sibling `claude` subprocesses running concurrently
on the same host — it listed the other warm ds-agent sessions) and correctly
reported that as a distinct, unrelated capability rather than conflating it
with the missing memory tools.

Root cause of the confusion itself: `agent_prompt.build_append_system_prompt()`
describes the memory tools in the system prompt *unconditionally* — that
text has no idea which MCP servers actually connected for this particular
process, so a stale-config process ends up with a system prompt promising
tools it was never given. Nothing to fix here (the prompt can't know what
it can't know); just a reminder that "the tool is in the system prompt" is
not evidence it's actually wired up — trust the CLI's own tool-not-found
error over the prompt text when debugging this class of report.

**Also confirmed in the wild**: this same session ran on
`google/gemma-4-31b-it` for these turns (18:49-18:51 UTC, post SDK-upgrade
to `claude-agent-sdk` 0.2.152 / CLI 2.1.259) and responded normally in a few
seconds each time — no hang. So the SDK upgrade (see the entry above it)
appears to have actually fixed the gemma-4-31b-it hang, even though the CLI
still logs the `[claude-code:unrecognized_model]` warning. Not exhaustively
verified across other unrecognized models, but a real positive data point.

**UPDATE — the memory tools stayed broken even after respawning; deep dive
(2026-09-06, later same day). ROOT CAUSE FOUND AND FIXED.** Killing the
stale process and letting it respawn (above) did NOT fix it. Root cause is
NOT the config-sync gap after all — that's real and worth still fixing, but
it isn't why `search_other_sessions` kept failing. Eight hypotheses tested and
ruled out, each with a direct empirical test on session
`c0c150377c624ab1` (with explicit go-ahead) plus isolated checks that
touched no live session:

1. *Stale per-session `.mcp.json`* — real (see above), fixed by respawn,
   but tools still failed afterward on the freshly-rendered config.
2. *`timeout: 30000` too short under load* — bumped to `90000` in
   `mcp.json`/`mcp.json.example`/live config; confirmed the bumped value
   actually reached the re-rendered per-session file; no effect.
3. *`"memory"` collides with the SDK's own reserved memory/CLAUDE.md concept*
   (`ClaudeAgentOptions.memory: Literal["user","project","local"]`, plus a
   `memory/` dir the CLI itself creates under `.claude/projects/<slug>/`) —
   renamed the mcp.json key to `agent_memory`; no effect.
4. *Host under heavy load* (self-inflicted by repeated test respawns;
   loadavg 2.56 on 4 cores at one point) — waited for loadavg < 1.0, retested
   under confirmed-idle conditions; no effect.
5. *`--resume=<uuid>` caches the tool manifest from first session start,
   independent of the OS process* — created a brand-new, non-resumed session
   (`18bd4fcb72594242`, later deleted) with a different, working model
   (`anthropic/claude-sonnet-4.5`, ruling out the gemma hang too); identical
   failure on the very first message.
6. *Tool not ready yet on the session's first turn* (MCP connection still
   establishing async in the background) — sent a benign first turn, waited
   for its result, tried the tool on turn 2; identical failure.
7. *MCP server count/registration-order limit* (6 servers declared; `memory`
   happened to be added last) — reordered `memory` to be the *first* key in
   `mcpServers`; identical failure.
8. *A bug in `agent_mcp.py` itself* — added temporary file-based
   instrumentation (writes to `/tmp/agent_mcp_debug.log`, bypassing the
   CLI's own stderr redirection — see below) directly into the module and
   `main()`, wrapping `server._handle_list_tools`. Confirmed: module import,
   `db.init()`, and `MCPServer(...)` construction all complete successfully,
   and `run_stdio_async()` is entered without raising — **but the wrapped
   `list_tools` handler is never called at all.** The real CLI never even
   asks this server for its tools. (Instrumentation was reverted after —
   `git checkout -- src/ds_agent/agent_mcp.py` — nothing shipped.)

**Side findings, still true regardless of the root cause**:
- `from mcp.server import MCPServer` (the third-party `mcp==2.1.1` package)
  costs 5-12 seconds to import by itself — confirmed via
  `python -X importtime`, dominated by eagerly-built Pydantic type schemas
  for *two* different MCP protocol versions
  (`mcp_types._v2026_07_28` ~2s, `mcp_types._v2025_11_25` ~1.4s). This is
  identical for `ds_mcp/server.py` and `research_mcp/server.py` (confirmed:
  `ds_mcp.server`'s own isolated handshake took 12.48s total, same order of
  magnitude as `agent_mcp.py`'s ~16s) — yet `ds`/`colab` tools are
  confirmed used successfully in real transcripts elsewhere on this host,
  so slow import alone doesn't explain a *guaranteed* failure. Whatever's
  different between "sometimes works" (ds/colab) and "never works"
  (memory, in every test run today) has not been isolated.
- The CLI subprocess's own stderr is inherited directly from the parent
  uvicorn process (`ClaudeAgentOptions` sets no `stderr=` callback, so
  `subprocess_cli.py` passes `stderr=None` → inherited fd) — this is how
  `[claude-code:unrecognized_model]` lines reach journalctl. But an
  individual **MCP child subprocess's** own stdout/stderr are both
  redirected to an internal socket (`lrwx ... -> socket:[N]`, confirmed via
  `/proc/<pid>/fd`) managed by the CLI's own subprocess plumbing, not
  inherited pipes — so nothing that server logs internally (via Python
  `logging`) is visible via journalctl. This is *why* file-based
  instrumentation (hypothesis 8 above) was necessary to see anything at all.
- `~/.claude.json` (global CLI config) has no `projects`/MCP-trust-related
  keys on this host — ruled out an MCP-server-approval-allowlist theory
  before it was even tested live.

**THE ACTUAL ROOT CAUSE**, found right after hypothesis 8 by finally
inspecting the *content* of the `system` frames the SDK streams (previously
only the frame `type` was ever printed during testing, never the payload —
an embarrassing miss given how much time the 8 hypotheses above cost).
The `system/init` frame's `mcp_servers` list starts every server at status
`"pending"`, and there's a genuine built-in tool, `WaitForMcpServers`, meant
for polling that. Calling it mid-session returned:

```
ready: false
Connected (their tools are now available — call them directly): memory
Still connecting (try again or proceed without): kaggle
```

So the CLI's own bookkeeping said `memory` **was** connected — yet the very
next call to bare `search_other_sessions` still failed with
`No such tool available`. That's the actual tell: the tool isn't missing,
it's *misnamed*. Claude Code namespaces every MCP tool as
`mcp__<server>__<tool>` (visible all along in successfully-used tool names
elsewhere on this host: `mcp__ds__ds_preview`, `mcp__kaggle__authorize`,
`mcp__colab__colab_auth`). Calling `mcp__memory__search_other_sessions`
directly worked immediately, returning real cross-session results.

**Why this wasn't a red herring the way `ds`/`colab` tools "just working"
seemed to disprove it**: a model gets each tool's *real, exact* name from
the structured tool-definitions block the API sends it (i.e.
`mcp__ds__ds_preview`), completely independent of whatever prose the system
prompt uses to describe it — normally a model just uses the real name
regardless of how the prompt phrases it. `agent_prompt.py`'s "Cross-session
memory + search" section, though, described the memory tools with literal,
parenthesized call syntax — `` `search_other_sessions(query)` `` — that
reads exactly like an authoritative function signature, not prose. That
was apparently enough to make the model call the bare name verbatim rather
than cross-checking its real tool list first — reproducible across models
too (`google/gemma-4-31b-it` **and** `anthropic/claude-sonnet-4.5` on a
brand-new, non-resumed session both made the identical mistake), so it's
not a model-quality issue, just a prompt that looked too exact to question.

**Fix** (`agent_prompt.py`): rewrote every memory-tool reference to use the
correct `mcp__memory__<tool>` names throughout, plus an explicit note
up front about why the prefix is required. Verified end-to-end with the
user's *exact original phrasing* ("check you can search other sessions",
no hints about tool names) on the same previously-broken session — the
model now correctly calls `mcp__memory__search_other_sessions` and confirms
success unprompted.

**Leftover, smaller, real findings from the investigation** (kept even
though they weren't the root cause):
- The config-sync gap (hypothesis 1 / the entry above this one) is real —
  fix stands.
- `mcp.json`'s `research`/`memory` timeout bump to `90000` is kept
  (harmless, defensively reasonable, and `research` was never actually
  verified to work at all before this — same missing-prefix bug likely
  affects any future `research`-server prose in the system prompt, so
  double-check tool names there too if `research_mcp` tools ever get
  added to the prompt).
- `from mcp.server import MCPServer` costs 5-12s to import (see side
  findings below) — real, but was a red herring for this specific bug.
- The lesson that generalizes: **when a documented/described MCP tool
  reports "No such tool available", check `WaitForMcpServers` and the
  `system/init` frame's `mcp_servers` list before assuming the server
  failed to connect — then try the fully-qualified `mcp__<server>__<tool>`
  name before spending hours on config/timing theories.**

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

## claude-agent-sdk was stale, and uv.lock wasn't actually locking it (2026-09-03)

Checked after suspecting the bundled CLI's model-recognition gaps
(gemma-4-31b-it, qwen3.8-27b — see entries above) might just be a version-lag
problem: the deployed `.venv` had `claude-agent-sdk==0.2.148`, but PyPI's
latest was `0.2.152` (released 2026-09-02, the day before). `pyproject.toml`
only requires `>=0.2.144`, so nothing was pinning it down — the venv was
simply last synced before 0.2.152 shipped.

**Bonus finding**: `uv.lock` was only 13 lines — just the `[[package]]` stanza
for `ds-agent` itself, none of its dependencies actually resolved/pinned.
A normal uv.lock for this project should have 40+ packages with hashes.
This means a fresh `uv sync` from a clean checkout (e.g. `deploy_remote.sh`
on a new host) wasn't getting a real reproducible resolution — every deploy
was silently re-resolving to "whatever's latest today" rather than a locked
set. Not clear how it got into that state; worth watching for regressions
(check `uv.lock` has real content after any future `uv add`/`uv sync`).

**Fix applied**: `uv sync --upgrade-package claude-agent-sdk` (as the `agent`
user, from `/opt/coding-agent`) — upgrades only that package (and its own
transitive deps: `anyio` 4.14.2→4.15.0, `sse-starlette` 3.4.8→3.4.10) rather
than a full re-resolution that could've bumped unrelated packages. This also
regenerated `uv.lock` properly (43 packages, real hashes) as a side effect.
Bundled CLI went `2.1.251` → `2.1.259`. Service restarted; came up clean.

**Update (2026-09-06)**: verified via real usage, not a synthetic test —
session `c0c150377c624ab1` ran several turns on `google/gemma-4-31b-it`
post-upgrade (18:49-18:51 UTC, see the "session-search-export" entry below)
and got normal replies in a few seconds each, no hang. So 2.1.259 appears to
actually handle this model fine now, despite still logging
`[claude-code:unrecognized_model]` (that log line is evidently just a stale
warning, not predictive of a hang on this CLI version). Not exhaustively
verified across every other unrecognized model — if you hit the hang again
on a *different* model, the mitigations above (watchdog + shared engine +
Telegram fallback message) are what carry the failure gracefully; they were
never a fix for the CLI's model list, just damage control.

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
