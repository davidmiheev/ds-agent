# Plan: DeepInfra MCP (sandboxes and GPU rentals as compute backends)

> Status: proposal, not implemented. Grounded in DeepInfra's own docs
> (`docs.deepinfra.com`, fetched as markdown via the site's `llms.txt` index)
> and its OpenAPI spec, not guessed. Nothing here has been run against a live
> account.

## 1. Two different products, not one

DeepInfra sells two compute things that matter here, and they are nothing
alike. The plan treats them separately.

| | Sandboxes | GPU Instances (container rentals) |
|---|---|---|
| What it is | Isolated Linux microVM, created with one API call | Dedicated GPU container with SSH, billed hourly |
| Hardware | CPU plans (`medium` default); no GPU is documented | B200 / B300 only, e.g. `1xB200-180GB`, `8xB200-180GB` |
| Billing | Per second, no minimum, only while creating/starting/running/stopping | Per hour, only while running |
| Access | HTTP API: streamed exec, file read/write | SSH (`ubuntu@<ip>`) only; no exec API documented |
| Lifetime guard | Idle timeout (default 1 h), 24 h running cap, 7-day retention after stop | None documented; terminate loses all data |
| Persistence | `/workspace` survives `stop`/`start`; the rest is reset | None; terminate deletes everything |

Auth is a single bearer key (`DEEPINFRA_API_KEY`), which fits the existing
BYOK vault as one `deepinfra` provider (`${VAULT:deepinfra}` in `mcp.json`),
with none of Colab's OAuth or Modal's two-value token.

## 2. Why this is worth doing

- **Sandboxes give the agent a remote place to run code that isn't this
  machine.** That is a safety feature, not only a capacity one: it is a
  credible way to run untrusted or model-written code away from the host,
  which is exactly what `docs/roadmap.md` P0 item 2 (`APP_PUBLIC=1` runs with
  `bypassPermissions`) is missing. Worth designing with that in mind.
- **Exec is a better fit for real jobs than Vast's.** Vast's command API has a
  ~20 s budget; the sandbox exec endpoint streams NDJSON and allows up to 30
  minutes per command, so the detached `nohup` + `tail` workaround is
  usually unnecessary.
- **Cost-leak risk is lower for sandboxes**: they idle out by default and
  stopped sandboxes cost nothing. The GPU containers are the opposite (see §4).
- **GPU Instances are a narrow offering**: B200/B300 only, up to 8 GPUs. That
  is the wrong tool for a quick experiment and an expensive one by default.
  It is only interesting for large fine-tuning or training runs the other
  backends can't hold.

## 3. Endpoints (from the OpenAPI spec)

Sandboxes, base `https://api.deepinfra.com`:
- `POST /v1/sandboxes` with `{plan, tags, timeout_seconds}` returns `{sandbox_id}`.
- `GET /v1/sandboxes`, `GET /v1/sandboxes/{id}`, `GET /v1/sandboxes/catalog`
  (plans and live pricing; the docs say not to hardcode numbers).
- `POST /v1/sandboxes/{id}/exec` with `{command: [argv...], timeout_seconds}`
  streams `application/x-ndjson`: `{"stdout": ...}` / `{"stderr": ...}` chunks,
  then exactly one terminal `{"returncode": N}` or `{"error": ...}`. The HTTP
  status is always 200 once streaming starts, so a client must read the last
  line, not the status code.
- `PUT`/`GET /v1/sandboxes/{id}/fs/content?path=/workspace/...`: raw bytes,
  scoped to `/workspace`, 100 MiB per call.
- `POST /v1/sandboxes/{id}/stop` and `/start`, `DELETE /v1/sandboxes/{id}`.

GPU rentals:
- `GET /v1/containers/gpu_availability` returns
  `gpus[{gpu_config, usd_per_hour, available, recommended}]`.
- `POST /v1/containers` with `{name, gpu_config, container_image,
  cloud_init_user_data}` returns `{container_id}`; the SSH public key goes in
  the cloud-init `ssh_authorized_keys`.
- `GET /v1/containers`, `GET /v1/containers/{id}`, `DELETE /v1/containers/{id}`.
- States: `creating`, `starting`, `running`, `shutting_down`, `failed`,
  `deleted`. As with Vast, a poll loop needs a timeout and must treat `failed`
  as terminal.

## 4. Proposed tool surface

Prefix `deepinfra_`, consistent with `colab_*` and `vast_*`.

Phase 1, sandboxes (HTTP only, no SSH):
`deepinfra_sandbox_new(plan, timeout_seconds)`, `deepinfra_sandbox_exec(command,
timeout_seconds)`, `deepinfra_sandbox_upload`, `deepinfra_sandbox_download`,
`deepinfra_sandbox_status`, `deepinfra_sandbox_stop`,
`deepinfra_sandbox_terminate`, `deepinfra_sandbox_plans`.

Phase 2, GPU containers (needs SSH):
`deepinfra_gpu_availability`, `deepinfra_gpu_new`, `deepinfra_gpu_status`,
`deepinfra_gpu_terminate`, plus an SSH-based exec and file-transfer path.

Cost-safety, as with Vast, is part of the design rather than a later patch:
- Sandboxes: set `timeout_seconds` explicitly on create (the default 1 h idle
  timeout cannot be disabled, and idle is measured from when the last call
  *finished*, so a long command can be stopped mid-run unless the timeout is
  generous). Tell the agent `stop` is free and `terminate` deletes
  `/workspace`.
- GPU containers: hourly price must be echoed on create, and `terminate` is
  the only way to stop billing; data is lost on terminate, so the agent must
  copy results out first.

## 5. Phasing

| Phase | Change | Needs SSH? |
|---|---|---|
| 1 | `src/deepinfra_mcp/server.py`: sandbox tools above, stdlib `urllib`, no new dependency, no dedicated venv (same approach as `vast_mcp`). Streamed exec parsed line by line, last line checked. BYOK `deepinfra` provider. | No |
| 2 (only if a workload needs B200/B300) | GPU container tools with SSH exec and transfer. Needs a registered keypair and an SSH client (system `ssh`/`scp` or `paramiko`). | Yes |
| 3 (speculative) | Wire sandboxes into the session runner as the isolated executor for `APP_PUBLIC` mode (see roadmap P0). | No |

## 6. Open questions and risks

- **No GPU sandboxes are documented.** If a sandbox can't see a GPU, Phase 1
  is a CPU and isolation backend, not an alternative to Colab or Vast for
  training. Confirm against the live catalog before positioning it.
- **The Python SDK (`deepinfra.Sandbox`) hasn't been inspected.** Phase 1
  uses the HTTP API directly to avoid a dependency, but the SDK should still
  be checked for pin conflicts (as `vastai` conflicted on `cryptography`) in
  case a later phase wants it.
- **Streaming under `urllib`.** Reading NDJSON incrementally is simple, but
  the MCP tool must still return a bounded result; very large outputs need
  truncation (the docs mention oversized output ends the command with an
  `error` line).
- **No live verification.** Sandbox creation is cheap and per-second, so a
  smoke test is far less risky than Vast's, but it still needs an account and
  the owner's go-ahead.
- **Related, separate question: DeepInfra as an LLM provider.** The OpenAPI
  spec tags chat completions as "OpenAI and Anthropic-compatible" and the docs
  list an "Anthropic SDK & Claude Code" integration, so the earlier concern
  (that only OpenAI-style endpoints exist) looks answerable. That is a
  `providers.py`/`model_catalog` change, tracked separately from this plan.

## 7. Recommendation

Do Phase 1 only: sandboxes. It is cheap, safe to smoke-test, needs no SSH, and
doubles as groundwork for isolating code execution in public mode. Defer GPU
containers until a specific training workload needs B200/B300, since that
path has the highest cost, the least guard rails, and the most setup.
