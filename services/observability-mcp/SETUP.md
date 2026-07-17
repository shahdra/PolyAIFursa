# observability-mcp — Setup & Run with Copilot (Part III)

Step-by-step to get the observability MCP server working in VS Code Copilot
agent mode. Two of the required files are **gitignored** (`.vscode/mcp.json` and
`services/observability-mcp/.env`), so after a fresh clone you must recreate them
from the contents below.

The server is a **local, stdio** MCP server: VS Code launches it via
`.vscode/mcp.json`, and it talks to:
- the **Compose deployment's Prometheus** on each EC2 box (dev/prod Elastic IPs, `:9090`), and
- the **Fluent Bit log buckets** in S3 (`shahd-polyai-logs-dev` / `-prod`).

---

## Prerequisites

- VS Code **1.99+** with **GitHub Copilot** + **Copilot Chat** extensions, signed in.
- The repo's venv at `./.venv` with deps installed:
  ```bash
  ./.venv/bin/pip install -r services/observability-mcp/requirements.txt
  ```
- Local AWS credentials that can read the log buckets (`s3:ListBucket`,
  `s3:GetObject`). These go in the `.env` below.
- The two EC2 boxes running (Prometheus on `:9090`), reachable from your machine.
  Current Elastic IPs: dev `3.225.53.28`, prod `3.232.101.124`.

---

## File 1 — `.vscode/mcp.json`  (GITIGNORED — recreate after clone)

Create `.vscode/mcp.json` at the repo root with exactly this:

```json
{
  "servers": {
    "observability": {
      "type": "stdio",
      "command": "${workspaceFolder}/.venv/bin/python",
      "args": ["${workspaceFolder}/services/observability-mcp/app.py"],
      "env": {
        "DEV_PROMETHEUS_URL": "http://3.225.53.28:9090",
        "PROD_PROMETHEUS_URL": "http://3.232.101.124:9090",
        "DEV_S3_LOGS_BUCKET": "shahd-polyai-logs-dev",
        "PROD_S3_LOGS_BUCKET": "shahd-polyai-logs-prod",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

Notes:
- `command` points at the repo venv so the server has `fastmcp`/`boto3`/`httpx`.
  `${workspaceFolder}` resolves to the repo root VS Code has open.
- Only **non-secret** config lives here (this file is not committed, but keep
  secrets out of it anyway). AWS credentials go in the `.env` (File 2).
- If the EC2 Elastic IPs ever change, update the two `*_PROMETHEUS_URL` values
  (they are EIPs, so this should be rare).

## File 2 — `services/observability-mcp/.env`  (GITIGNORED — recreate after clone)

Create `services/observability-mcp/.env` with real AWS credentials. `app.py`
loads this file on startup (via `load_dotenv`), so the S3 log tools can auth:

```
DEV_PROMETHEUS_URL=http://3.225.53.28:9090
PROD_PROMETHEUS_URL=http://3.232.101.124:9090

DEV_S3_LOGS_BUCKET=shahd-polyai-logs-dev
PROD_S3_LOGS_BUCKET=shahd-polyai-logs-prod

AWS_REGION=us-east-1

AWS_ACCESS_KEY_ID=<real key>
AWS_SECRET_ACCESS_KEY=<real secret>
```

- Use the same AWS keys the other services use (they can read the buckets).
- Values here override / back up the `mcp.json` env; the credentials are the part
  that MUST be here (never in `mcp.json`).
- `.env.example` in this folder is the committed template to copy from.

---

## Load & use in VS Code Copilot

1. **Open the repo root folder** in VS Code (so `.vscode/mcp.json` is detected).
2. Open `.vscode/mcp.json` → click the **Start** CodeLens above `"observability"`.
   (Or Command Palette → **MCP: List Servers** → `observability` → **Start**.)
   - After editing `app.py` or `.env`, use **Restart** so changes take effect.
3. Open **Copilot Chat**, set the mode dropdown to **Agent**.
4. Ask it to use the server. Good first smoke test (no AWS needed):
   > Use the observability server to run the Prometheus query `up` in dev.
5. Then the task's prompts:
   - "What containers are shipping logs to S3 in dev?"
   - "Show me the logs of the yolo service in dev for the last 5 minutes."
   - "Show me the CPU usage of the prod instance for the last 10 minutes."
   - "What happened in dev around <time>? A client got an internal server error."

Copilot discovers the tools automatically and will ask permission to run each —
approve it. You do **not** need to find a "tools" icon; just ask in Agent mode.