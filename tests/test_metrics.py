"""Tests for the Prometheus self-metrics plumbing.

We assert against deltas read from the registry (not absolute values), because
prometheus_client uses a process-global default registry that other tests in the
same run also touch. The two /metrics endpoints are checked for the text
exposition format Prometheus expects.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from releasepulse.api.main import app as api_app
from releasepulse.metrics import CONTENT_TYPE_LATEST, checks_total, generate_latest
from releasepulse.worker.http import WorkerState, create_app


def test_counter_increment_is_visible_in_the_registry() -> None:
    before = REGISTRY.get_sample_value("sentinel_checks_total", {"result": "success"}) or 0.0
    checks_total.labels(result="success").inc()
    after = REGISTRY.get_sample_value("sentinel_checks_total", {"result": "success"})
    assert after == before + 1


def test_generate_latest_emits_help_and_type_lines() -> None:
    checks_total.labels(result="failure").inc()  # ensure the family has a sample
    body = generate_latest().decode()
    assert "# HELP sentinel_checks_total" in body
    assert "# TYPE sentinel_checks_total counter" in body


def test_api_metrics_endpoint_serves_exposition() -> None:
    resp = TestClient(api_app).get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == CONTENT_TYPE_LATEST


def test_worker_metrics_endpoint_serves_exposition() -> None:
    app = create_app(WorkerState(), heartbeat_max_age=timedelta(seconds=180))
    resp = TestClient(app).get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == CONTENT_TYPE_LATEST
