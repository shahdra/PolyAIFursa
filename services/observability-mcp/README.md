# observability-mcp

A local (stdio) MCP server for VS Code Copilot to query the PolyAI stack's
**container logs** (shipped to S3 by Fluent Bit) and **metrics** (Prometheus),
across the `dev` and `prod` environments.

Unlike `img-proc-mcp` (which runs in the deployment over HTTP), this server runs
on your machine and speaks MCP over stdio — VS Code Copilot launches it via
`.vscode/mcp.json`.

## Tools

| Tool | What it does |
|------|--------------|
| `get_container_logs(env, service?, since_minutes, limit)` | Fetch recent logs from the env's S3 log bucket; optional `service` substring filter (e.g. `yolo`, `agent`, an error string) |
| `list_log_activity(env, since_minutes)` | Summarize what's shipping logs: objects, record count, per-host and per-stream counts |
| `query_prometheus(env, promql)` | Instant PromQL query (`/api/v1/query`) |
| `query_prometheus_range(env, promql, since_minutes, step_seconds)` | Ranged PromQL query (`/api/v1/query_range`) for time-series |
| `list_prometheus_metrics(env)` | List available metric names |

`env` is `dev` or `prod`.

> Note: Fluent Bit ships the raw Docker JSON records (with a `host` field) but not
> the friendly container name, so `service` filtering matches the **log text**
> (best-effort), not a container-name field.

## Setup

The MCP queries the **Compose deployment's** Prometheus on each EC2 box
(Elastic IPs, port 9090) — `dev` → `shahd-yolo-dev-server-3`, `prod` →
`shahd-yolo-prod-server`. These are reachable directly; no port-forward needed.

1. Install deps: `pip install -r requirements.txt`
2. Ensure AWS credentials can read the log buckets (env vars or your AWS profile).
3. VS Code Copilot picks up `.vscode/mcp.json` automatically (env is set there).

## Example prompts (Copilot agent mode)

- "Show me the logs of the yolo service container for the last 5 minutes"
- "Show me the CPU usage of the prod instance for the last 10 minutes"
- "What containers are shipping logs to S3?"
- "What happened to the yolo service at <time>? The client got an internal server error"

## Tests

```bash
pytest
```
