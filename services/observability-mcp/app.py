# services/observability-mcp/app.py
#
# Local (stdio) MCP server for VS Code Copilot: query container logs shipped to
# S3 by Fluent Bit, and metrics from the dev/prod Prometheus instances.
#
# Runs on the developer's machine via `.vscode/mcp.json` (transport: stdio), so
# it uses mcp.run() with the default stdio transport (NOT the HTTP transport the
# img-proc-mcp server uses in the compose/k8s deployment).
#
# Environment (see .env.example / .vscode/mcp.json):
#   DEV_PROMETHEUS_URL / PROD_PROMETHEUS_URL   the Compose Prometheus on each EC2
#                                              box, e.g. http://<eip>:9090
#   DEV_S3_LOGS_BUCKET / PROD_S3_LOGS_BUCKET   Fluent Bit log buckets
#   AWS_REGION                                 default us-east-1
import gzip
import json
import os
from datetime import datetime, timedelta, timezone

import boto3
import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

# Load a .env sitting next to this file (AWS creds, bucket/URL overrides). VS
# Code launches this over stdio and does not source .env itself, and the values
# in .vscode/mcp.json cover only non-secret config — so credentials live here,
# in the gitignored services/observability-mcp/.env.
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

mcp = FastMCP("observability")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
s3_client = boto3.client("s3", region_name=AWS_REGION)

# env name -> (prometheus url, s3 logs bucket)
_ENVS = {
    "dev": (
        os.environ.get("DEV_PROMETHEUS_URL", "http://3.225.53.28:9090"),
        os.environ.get("DEV_S3_LOGS_BUCKET", ""),
    ),
    "prod": (
        os.environ.get("PROD_PROMETHEUS_URL", "http://3.232.101.124:9090"),
        os.environ.get("PROD_S3_LOGS_BUCKET", ""),
    ),
}


def _resolve(env: str):
    """Map an env name ('dev'/'prod') to its (prometheus_url, logs_bucket)."""
    key = (env or "").strip().lower()
    if key not in _ENVS:
        raise ValueError(f"unknown env '{env}'; expected 'dev' or 'prod'")
    return _ENVS[key]


# Objects can hold records written slightly before the object's upload time, so
# widen the LastModified pre-filter by this margin on each side.
_OBJECT_TIME_MARGIN = timedelta(minutes=2)


# ---------------------------------------------------------------------------
# S3 log helpers
# ---------------------------------------------------------------------------
def _day_prefixes(start: datetime, end: datetime) -> list[str]:
    """Fluent Bit writes keys as logs/YYYY/MM/DD/...; return every day-prefix
    covering [start, end] (usually one, more if the range spans midnights)."""
    prefixes = []
    day = start.date()
    while day <= end.date():
        prefixes.append(f"logs/{day.year:04d}/{day.month:02d}/{day.day:02d}/")
        day += timedelta(days=1)
    return prefixes


def _list_objects(bucket: str, start: datetime, end: datetime) -> list[dict]:
    """List log objects whose LastModified overlaps [start, end] (± margin).

    Pre-filtering on object metadata avoids downloading every object in the day
    when the caller only wants a short window.
    """
    lo, hi = start - _OBJECT_TIME_MARGIN, end + _OBJECT_TIME_MARGIN
    out: dict[str, dict] = {}
    paginator = s3_client.get_paginator("list_objects_v2")
    for prefix in _day_prefixes(start, end):
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                mod = obj.get("LastModified")
                if mod is None:
                    continue
                mod = mod.astimezone(timezone.utc)
                if lo <= mod <= hi:
                    out[obj["Key"]] = obj
    return sorted(out.values(), key=lambda o: o["LastModified"])


def _iter_log_records(bucket: str, start: datetime, end: datetime):
    """Yield parsed NDJSON records from the gzipped log objects overlapping the
    [start, end] window (objects pre-filtered by LastModified).

    Each Fluent Bit S3 record looks like:
      {"date": <epoch>, "log": "<line>", "stream": "stdout", "host": "<host>"}
    Per-record time filtering is left to callers using _record_ts.
    """
    for obj in _list_objects(bucket, start, end):
        body = s3_client.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
        try:
            text = gzip.decompress(body).decode("utf-8", errors="replace")
        except (OSError, EOFError):
            text = body.decode("utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield _unwrap(json.loads(line))
            except json.JSONDecodeError:
                # tolerate a stray non-JSON line rather than aborting
                yield {"log": line}


def _unwrap(rec: dict) -> dict:
    """Flatten a doubly-encoded record.

    Fluent Bit's docker parser re-wraps the already-JSON Docker log line, so a
    record can look like:
      {"date":..., "host":..., "log": "{\\"log\\":..., \\"stream\\":...,
                                        \\"attrs\\":{\\"container_name\\":...}}"}
    Merge the inner object's fields up (real log/stream/attrs/time) while keeping
    the outer 'date' and 'host'. No-op for already-flat records.
    """
    inner_str = rec.get("log")
    if isinstance(inner_str, str):
        s = inner_str.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                inner = json.loads(s)
            except json.JSONDecodeError:
                return rec
            if isinstance(inner, dict):
                merged = {**inner}
                # preserve outer envelope fields the inner object lacks
                if "host" in rec and "host" not in merged:
                    merged["host"] = rec["host"]
                if "date" in rec:
                    merged.setdefault("_outer_date", rec["date"])
                return merged
    return rec


def _record_ts(rec: dict) -> float | None:
    """Best-effort epoch seconds for a record.

    Handles Fluent Bit's numeric 'date', and ISO 'time'/'timestamp' strings —
    including Docker's nanosecond precision, which datetime.fromisoformat can't
    parse (it accepts at most 6 fractional digits)."""
    for k in ("time", "date", "timestamp", "_outer_date"):
        v = rec.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str) and v:
            s = v
            # Trim sub-second precision to microseconds and normalize the tz.
            if "." in s:
                head, frac = s.split(".", 1)
                frac = frac.rstrip("Z")
                # frac may carry an explicit offset like "123456789+00:00"
                off = ""
                for sign in ("+", "-"):
                    if sign in frac:
                        frac, off = frac.split(sign, 1)
                        off = sign + off
                        break
                s = f"{head}.{frac[:6]}{off or '+00:00'}"
            elif s.endswith("Z"):
                s = s[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    return None


# Bounds for tool arguments (validated, with clear errors on violation).
_MAX_MINUTES = 1440       # 24h lookback cap
_MAX_LIMIT = 2000         # max lines returned


def _validate(name: str, value: int, lo: int, hi: int) -> None:
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")


def _normalize_name(name: str) -> str:
    """Normalize a container/service name for matching.
    yolo-service -> yolo, services-yolo-1 -> yolo, yolo_service -> yolo."""
    n = (name or "").strip().lower().replace("_", "-").lstrip("/")
    if n.startswith("services-"):
        n = n[len("services-"):]
    if n.endswith("-service"):
        n = n[: -len("-service")]
    parts = n.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():  # strip a compose replica suffix
        n = parts[0]
    return n


def _record_container(rec: dict) -> str:
    """Container name from a fluentd-driver record. Docker sends it as top-level
    `container_name` (leading '/'); some setups nest it under `attrs`."""
    for key in ("container_name", "container"):
        v = rec.get(key)
        if isinstance(v, str) and v:
            return v.lstrip("/")
    attrs = rec.get("attrs")
    if isinstance(attrs, dict):
        v = attrs.get("container_name")
        if isinstance(v, str) and v:
            return v.lstrip("/")
    return ""


# ---------------------------------------------------------------------------
# Tools — logs
# ---------------------------------------------------------------------------
def _collect_logs(
    env: str, service: str | None, start: datetime, end: datetime, limit: int
) -> dict:
    """Read log records in [start, end], optionally filtered by `service`, and
    return a structured result envelope.

    Since the fluentd log driver enriches records with `container_name`, a
    `service` filter matches on the normalized container name (yolo-service ==
    yolo). If a record carries no container name, it falls back to a log-text
    substring match so nothing is silently dropped.
    """
    _, bucket = _resolve(env)
    if not bucket:
        return {"success": False, "env": env, "error": f"no log bucket configured for env '{env}'"}

    want = _normalize_name(service) if service else None
    lo, hi = start.timestamp(), end.timestamp()
    out = []
    for rec in _iter_log_records(bucket, start, end):
        ts = _record_ts(rec)
        if ts is not None and not (lo <= ts <= hi):
            continue
        container = _record_container(rec)
        line = str(rec.get("log", ""))
        if want:
            if container:
                if _normalize_name(container) != want:
                    continue
            elif want not in line.lower():  # fallback for un-enriched records
                continue
        out.append(
            {
                "time": datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None,
                "container": container or None,
                "host": rec.get("host"),
                # fluentd driver uses 'source'; json-file used 'stream'
                "source": rec.get("source") or rec.get("stream"),
                "log": line,
            }
        )
    out.sort(key=lambda r: r["time"] or "")
    return {
        "success": True,
        "env": env,
        "service": service,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "count": len(out),
        "lines": out[-limit:],
    }


@mcp.tool()
def get_container_logs(
    env: str = "dev",
    service: str | None = None,
    since_minutes: int = 5,
    limit: int = 200,
) -> str:
    """Fetch recent container logs from the env's S3 log bucket.

    env: 'dev' or 'prod'. service: optional substring to filter log lines by
    (e.g. 'yolo', 'agent', an error string) — matched against the log text.
    since_minutes: how far back to look (1-1440). limit: max lines (1-2000).
    Returns JSON {"success","count","lines":[{"time","host","stream","log"}...]}.
    """
    try:
        _validate("since_minutes", since_minutes, 1, _MAX_MINUTES)
        _validate("limit", limit, 1, _MAX_LIMIT)
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=since_minutes)
        return json.dumps(_collect_logs(env, service, start, end, limit), default=str)
    except (ValueError, Exception) as exc:  # noqa: BLE001 - report, don't crash the tool
        return json.dumps({"success": False, "env": env, "error": str(exc)})


@mcp.tool()
def get_container_logs_at_time(
    timestamp: str,
    env: str = "dev",
    service: str | None = None,
    window_minutes: int = 5,
    limit: int = 300,
) -> str:
    """Investigate what happened around a specific time (incident debugging).

    timestamp: ISO-8601, e.g. '2026-07-01T12:00:00Z'. Searches
    [timestamp - window_minutes, timestamp + window_minutes]. env: 'dev'/'prod'.
    service: optional log-text filter (e.g. 'yolo', 'error'). limit: max lines.
    Returns the same envelope as get_container_logs.
    """
    try:
        _validate("window_minutes", window_minutes, 1, 120)
        _validate("limit", limit, 1, _MAX_LIMIT)
        center = _record_ts({"time": timestamp})
        if center is None:
            raise ValueError("timestamp must be ISO-8601, e.g. '2026-07-01T12:00:00Z'")
        center_dt = datetime.fromtimestamp(center, timezone.utc)
        start = center_dt - timedelta(minutes=window_minutes)
        end = center_dt + timedelta(minutes=window_minutes)
        return json.dumps(_collect_logs(env, service, start, end, limit), default=str)
    except (ValueError, Exception) as exc:  # noqa: BLE001
        return json.dumps({"success": False, "env": env, "timestamp": timestamp, "error": str(exc)})


@mcp.tool()
def list_log_activity(env: str = "dev", since_minutes: int = 60) -> str:
    """Summarize what is shipping logs to S3 in the given window: distinct
    containers, hosts, source counts, and object count. Answers 'what containers
    are shipping logs to S3?'. env: 'dev' or 'prod'. since_minutes: 1-1440.
    Returns JSON {"success","objects","records","containers":{...},"hosts":{...},"streams":{...}}.
    """
    try:
        _validate("since_minutes", since_minutes, 1, _MAX_MINUTES)
        _, bucket = _resolve(env)
        if not bucket:
            return json.dumps({"success": False, "env": env, "error": f"no log bucket configured for env '{env}'"})

        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=since_minutes)
        lo, hi = start.timestamp(), end.timestamp()
        containers: dict[str, int] = {}
        hosts: dict[str, int] = {}
        streams: dict[str, int] = {}
        objects = {o["Key"] for o in _list_objects(bucket, start, end)}
        records = 0
        for rec in _iter_log_records(bucket, start, end):
            ts = _record_ts(rec)
            if ts is not None and not (lo <= ts <= hi):
                continue
            records += 1
            c = _record_container(rec)
            if c:
                containers[c] = containers.get(c, 0) + 1
            h = rec.get("host")
            if h:
                hosts[h] = hosts.get(h, 0) + 1
            st = rec.get("source") or rec.get("stream")
            if st:
                streams[st] = streams.get(st, 0) + 1
        return json.dumps(
            {"success": True, "env": env, "objects": len(objects), "containers": containers,
             "records": records, "hosts": hosts, "streams": streams}
        )
    except (ValueError, Exception) as exc:  # noqa: BLE001
        return json.dumps({"success": False, "env": env, "error": str(exc)})


# ---------------------------------------------------------------------------
# Tools — Prometheus
# ---------------------------------------------------------------------------
@mcp.tool()
def query_prometheus(env: str = "dev", promql: str = "up") -> str:
    """Run an instant PromQL query against the env's Prometheus (/api/v1/query).
    env: 'dev' or 'prod'. promql: e.g. 'up', 'rate(http_requests_total[5m])'.
    Returns JSON {"success","result":[...],"error"}.
    """
    try:
        url, _ = _resolve(env)
        r = httpx.get(f"{url}/api/v1/query", params={"query": promql}, timeout=30)
        r.raise_for_status()
        payload = r.json()
        return json.dumps(
            {"success": payload.get("status") == "success", "env": env, "query": promql,
             "result": payload.get("data", {}).get("result", []), "error": payload.get("error")}
        )
    except (httpx.HTTPError, ValueError, Exception) as exc:  # noqa: BLE001
        return json.dumps({"success": False, "env": env, "query": promql, "error": str(exc)})


@mcp.tool()
def query_prometheus_range(
    env: str = "dev",
    promql: str = "up",
    since_minutes: int = 10,
    step_seconds: int = 15,
) -> str:
    """Run a ranged PromQL query over the last `since_minutes` (/api/v1/query_range).
    Use for time-series like 'CPU usage over the last 10 minutes'. env: 'dev'/'prod'.
    since_minutes: 1-1440. step_seconds: 5-3600.
    Returns JSON {"success","result":[...],"error"}.
    """
    try:
        _validate("since_minutes", since_minutes, 1, _MAX_MINUTES)
        _validate("step_seconds", step_seconds, 5, 3600)
        url, _ = _resolve(env)
        end = datetime.now(timezone.utc).timestamp()
        start = end - since_minutes * 60
        r = httpx.get(
            f"{url}/api/v1/query_range",
            params={"query": promql, "start": start, "end": end, "step": step_seconds},
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
        return json.dumps(
            {"success": payload.get("status") == "success", "env": env, "query": promql,
             "result": payload.get("data", {}).get("result", []), "error": payload.get("error")}
        )
    except (httpx.HTTPError, ValueError, Exception) as exc:  # noqa: BLE001
        return json.dumps({"success": False, "env": env, "query": promql, "error": str(exc)})


@mcp.tool()
def list_prometheus_metrics(env: str = "dev") -> str:
    """List available metric names in the env's Prometheus (label __name__).
    Useful before writing a PromQL query. env: 'dev' or 'prod'.
    Returns JSON {"success","metrics":[...],"error"}.
    """
    try:
        url, _ = _resolve(env)
        r = httpx.get(f"{url}/api/v1/label/__name__/values", timeout=30)
        r.raise_for_status()
        payload = r.json()
        return json.dumps({"success": payload.get("status") == "success", "env": env,
                           "metrics": payload.get("data", [])})
    except (httpx.HTTPError, ValueError, Exception) as exc:  # noqa: BLE001
        return json.dumps({"success": False, "env": env, "error": str(exc)})


if __name__ == "__main__":
    # stdio transport: VS Code Copilot launches this via `python app.py` and
    # speaks MCP over stdin/stdout.
    mcp.run()
