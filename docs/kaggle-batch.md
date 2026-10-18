# Kaggle batch runner

`src/ds_agent/kaggle_batch.py` is a persistent queue of Kaggle script
notebooks built on Kaggle's own MCP tools: `save_notebook`,
`get_notebook_session_status`, `list_notebook_session_output` and
`cancel_notebook_session`. The kaggle proxy (`kaggle_mcp.py`) lists five
local tools next to Kaggle's:

| tool | does |
| --- | --- |
| `batch_create` | a named batch of jobs (`slug`, `script` or `script_path`, `out_dir`, `meta`), options `max_running` (default 5), `log_glob`, `download_glob`, `done_regex`, `fail_regex`, `deadline_regex`, `private`, `internet`; pushes nothing |
| `batch_step` | refreshes running jobs; downloads the outputs of finished ones and checks their logs; pushes queued jobs while fewer than `max_running` run across **all** batches in the directory |
| `batch_status` | a batch's jobs and log checks, or every batch's counts (no Kaggle call) |
| `batch_retry` | requeues an ended job (the next push is a new version of the same notebook) |
| `batch_cancel` | drops a queued job, or cancels a running one by its kernel session id (taken from its output urls) |

Job states: `queued`, then `running`, then `complete`, `error` or
`cancelled`, then `collected`. After collection, `outcome` is one of:

- `ok`: every log has the done marker, and none has the fail or deadline
  marker;
- `failures`;
- `no-logs`;
- `error`.

State lives in `KAGGLE_BATCH_DIR` (default `~/.kaggle_batch/`), one JSON per
batch. Tokens are never stored there.

Every Kaggle-facing function takes an async `call_tool(name, arguments)`.
The proxy passes its upstream session; `JsonRpcKaggle` is a direct HTTPS
JSON-RPC client for command-line use without the `mcp` package. The
`save_notebook` corrections live in `kaggle_requests.py`, shared by the
proxy and the batch runner.

Tests: `PYTHONPATH=src python tests/test_kaggle_batch.py` uses a fake
Kaggle. The cap holds across batches, logs are checked, push failures and
retries work, and slugs cannot clash. A live self-test (phh_strategy,
2026-10-17) pushed a notebook, saw it complete, downloaded its two logs and
flagged the one written to fail.

phh_strategy vendors these files in `tools/mcp/` and wraps them for its
contests in `scripts/kaggle/kbatch.py` (one unit per (contract, session),
merging results into its cache).
