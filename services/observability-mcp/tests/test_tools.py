import gzip
import json
import time
from datetime import datetime, timezone

import pytest


@pytest.fixture
def app(monkeypatch):
    """Import the server with env set and a fake S3 client holding one gzipped
    log object of fluentd-driver-shaped NDJSON records (carry container_name)."""
    monkeypatch.setenv("DEV_S3_LOGS_BUCKET", "shahd-polyai-logs-dev")
    monkeypatch.setenv("PROD_S3_LOGS_BUCKET", "shahd-polyai-logs-prod")
    monkeypatch.setenv("DEV_PROMETHEUS_URL", "http://localhost:9090")
    monkeypatch.setenv("PROD_PROMETHEUS_URL", "http://localhost:9091")
    import app as mod

    now = time.time()
    # json-file + labels shape: container name lives in attrs.container_name,
    # stdout/stderr in `stream`.
    recs = [
        {"date": now - 30, "log": "prediction done", "stream": "stdout",
         "attrs": {"container_name": "yolo-service"}, "host": "fb1"},
        {"date": now - 9999, "log": "old line", "stream": "stdout",
         "attrs": {"container_name": "agent-service"}, "host": "fb1"},
        {"date": now - 20, "log": "internal server error", "stream": "stderr",
         "attrs": {"container_name": "agent-service"}, "host": "fb1"},
    ]
    blob = gzip.compress(("\n".join(json.dumps(r) for r in recs)).encode())

    class Body:
        def read(self):
            return blob

    class Paginator:
        def paginate(self, **_):
            return [{"Contents": [{"Key": "logs/2026/07/17/yolo_1.gz",
                                   "LastModified": datetime.now(timezone.utc)}]}]

    class FakeS3:
        def get_paginator(self, _):
            return Paginator()

        def get_object(self, **_):
            return {"Body": Body()}

    monkeypatch.setattr(mod, "s3_client", FakeS3())
    return mod


def test_resolve_valid_and_invalid(app):
    assert app._resolve("dev")[1] == "shahd-polyai-logs-dev"
    with pytest.raises(ValueError):
        app._resolve("staging")


def test_normalize_name(app):
    assert app._normalize_name("yolo-service") == "yolo"
    assert app._normalize_name("services-yolo-1") == "yolo"
    assert app._normalize_name("yolo_service") == "yolo"
    assert app._normalize_name("/agent") == "agent"


def test_record_container_variants(app):
    assert app._record_container({"container_name": "/yolo"}) == "yolo"
    assert app._record_container({"attrs": {"container_name": "agent"}}) == "agent"
    assert app._record_container({"log": "x"}) == ""


def test_record_ts_handles_nanoseconds(app):
    assert app._record_ts({"time": "2026-07-17T12:00:00.123456789Z"}) is not None
    assert app._record_ts({"date": 1752750000.5}) == 1752750000.5
    assert app._record_ts({"log": "x"}) is None


def test_filter_by_container_name_exact(app):
    # "yolo-service" must match container "/yolo" (normalized) and NOT the agent
    r = json.loads(app.get_container_logs(env="dev", service="yolo-service", since_minutes=5))
    assert r["success"] is True
    assert r["count"] == 1
    assert r["lines"][0]["container"] == "yolo-service"


def test_filter_agent_gets_only_agent(app):
    # bare "agent" must match container "agent-service" via normalization
    r = json.loads(app.get_container_logs(env="dev", service="agent", since_minutes=5))
    # only the recent agent line (old one out of window)
    assert r["count"] == 1
    assert "internal server error" in r["lines"][0]["log"]


def test_excludes_out_of_window(app):
    r = json.loads(app.get_container_logs(env="dev", since_minutes=5))
    assert r["count"] == 2  # the -9999s agent line dropped


def test_validates_args(app):
    r = json.loads(app.get_container_logs(env="dev", since_minutes=0))
    assert r["success"] is False and "since_minutes" in r["error"]


def test_logs_at_time_bad_timestamp(app):
    r = json.loads(app.get_container_logs_at_time(timestamp="nope", env="dev"))
    assert r["success"] is False and "ISO-8601" in r["error"]


def test_list_log_activity_reports_containers(app):
    r = json.loads(app.list_log_activity(env="dev", since_minutes=5))
    assert r["success"] is True
    assert r["records"] == 2
    assert r["containers"] == {"yolo-service": 1, "agent-service": 1}  # old agent line excluded
    assert r["streams"] == {"stdout": 1, "stderr": 1}


def test_missing_bucket_returns_error(app):
    app._ENVS["prod"] = ("http://localhost:9091", "")
    r = json.loads(app.get_container_logs(env="prod", since_minutes=5))
    assert r["success"] is False and "bucket" in r["error"]
