# CI/CD: GitHub Actions Deploy Workflow

`.github/workflows/deploy.yml` runs `scripts/deploy_remote.sh` from GitHub
Actions — on every push to `main`, or on demand from the **Actions** tab
(`workflow_dispatch`), where the target host/user/port/dir/app-port can be
overridden for that single run.

Configure these **repository secrets** (Settings → Secrets and variables →
Actions) before running it:

| Secret | Required | Purpose |
|---|---|---|
| `SSH_PRIVATE_KEY` | ✅ | Private key for the deploy SSH user |
| `DEPLOY_HOST` | ✅ | Default remote IP/hostname (overridable per-run via the `remote_host` input) |
| `ENV_FILE` | ✅ | Full contents of the `.env` file to deploy |
| `REMOTE_USER` | optional | SSH user (default `root`) |
| `REMOTE_PORT` | optional | SSH port (default `22`) |
| `REMOTE_DIR` | optional | Install dir on remote (default `/opt/coding-agent`) |
| `APP_PORT` | optional | Web server port (default `8765`) |
| `COLAB_TOKEN_JSON` | optional | Contents of `~/.config/colab-cli/token.json`, copied to the remote so Colab MCP skips a fresh OAuth flow |

> **Note on Colab OAuth:** Colab MCP requires a Google OAuth token (`~/.config/colab-cli/token.json`).
> Run `src/colab_mcp/.venv/bin/python src/colab_mcp/auth_once.py` locally once. The deploy script (`scripts/deploy_remote.sh`) automatically copies your local token to `/home/agent/.config/colab-cli/token.json` during deployment.

See `README.md` → *One-Command Remote Deployment* for what `deploy_remote.sh`
itself does (rsync, Node/uv/venv setup, systemd service, health checks) — the
workflow above is a thin wrapper around that same script, sourcing its
arguments from the secrets table instead of interactive prompts.
