"""End-to-end acceptance test for the whole loop (Phase 0B done-when).

Drives the real code paths in-process: the demo target's latency behaviour, the
real check path (perform_check/check_and_record), the webhook handler with its
idempotency, the auto-evaluation finder + detector, and alert dispatch. Only two
non-product things are stubbed: the SSRF resolver (a public-IP stand-in, as in
test_worker_check) and the AlertSender (the capturing fake from test_alerting).

Both apps are reached over httpx.ASGITransport - no sockets - but the demo's
asyncio.sleep still elapses real time, so the latency regression is real.
Windows are shortened via TEST_THRESHOLDS instead of waiting.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import time
from datetime import timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from releasepulse.alerting.dispatch import dispatch_pending_alerts
from releasepulse.api.deps import get_db
from releasepulse.api.main import app as api_app
from releasepulse.api.webhook_auth import (
    EVENT_ID_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    expected_signature,
)
from releasepulse.config import Settings, get_settings
from releasepulse.demo.service import create_app as create_demo_app
from releasepulse.detector.core import Thresholds
from releasepulse.detector.service import evaluate_due_deployments
from releasepulse.models import Alert, Deployment, Endpoint, Incident, Service
from releasepulse.worker.runner import check_and_record
from tests.test_alerting import FakeAlertSender

WEBHOOK_SECRET = "test-webhook-secret"


def _signed_headers(body: bytes, *, event_id: str) -> dict[str, str]:
    """Build valid HMAC headers for raw `body`, signed with a fresh timestamp."""
    ts = str(int(time.time()))
    return {
        TIMESTAMP_HEADER: ts,
        SIGNATURE_HEADER: expected_signature(WEBHOOK_SECRET, ts, body),
        EVENT_ID_HEADER: event_id,
        "Content-Type": "application/json",
    }

# Real spec maths, shrunk windows so checks made seconds apart land correctly.
TEST_THRESHOLDS = Thresholds(
    baseline_window=timedelta(minutes=5),
    warmup=timedelta(0),
    observation_window=timedelta(minutes=5),
    min_samples=10,
    min_successful_baseline=5,
)


def _public_resolver(host, port):
    return [ipaddress.ip_address("93.184.216.34")]


def _count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def test_e2e_deploy_regression_fires_one_alert_idempotently(db, engine) -> None:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    demo_app = create_demo_app()

    svc = Service(name="demo-svc")
    db.add(svc)
    db.flush()
    endpoint = Endpoint(
        service_id=svc.id,
        environment="production",
        url="http://demo.local/",
        method="GET",
        expected_status=200,
        timeout_sec=10,
        check_interval_sec=30,
        enabled=True,
    )
    db.add(endpoint)
    db.commit()

    payload = {
        "service": "demo-svc",
        "environment": "production",
        "version": "v2",
        "source": "manual",
        "external_id": "deploy-1",
    }

    async def _set_mode(client, *, latency_ms, error_rate) -> None:
        r = await client.post(
            "http://demo/admin/mode",
            json={"latency_ms": latency_ms, "error_rate": error_rate},
        )
        assert r.status_code == 200, r.text

    async def _run_checks(client, n) -> None:
        for _ in range(n):
            await check_and_record(
                db, endpoint, client,
                app_env="production", allowlist_raw="", resolver=_public_resolver,
            )

    async def _baseline_deploy_degrade() -> None:
        demo = httpx.AsyncClient(transport=httpx.ASGITransport(app=demo_app), base_url="http://demo")
        api = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://api")
        async with demo, api:
            # Healthy baseline.
            await _set_mode(demo, latency_ms=5, error_rate=0.0)
            await _run_checks(demo, 12)
            # The deploy event, signed (HMAC) over the exact bytes we send.
            body = json.dumps(payload).encode()
            r = await api.post(
                "/webhooks/deployments",
                content=body,
                headers=_signed_headers(body, event_id="evt-deploy-1"),
            )
            assert r.status_code == 201, r.text
            # Degrade: much slower after the deploy.
            await _set_mode(demo, latency_ms=150, error_rate=0.0)
            await _run_checks(demo, 12)

    async def _resend_webhook() -> int:
        api = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://api")
        async with api:
            # A CI rerun: a fresh signed delivery (new event id) for the same
            # deployment. Dedups on (source, external_id) -> 200, no new row.
            body = json.dumps(payload).encode()
            r = await api.post(
                "/webhooks/deployments",
                content=body,
                headers=_signed_headers(body, event_id="evt-deploy-2"),
            )
            return r.status_code

    def _override_db():
        yield db

    api_app.dependency_overrides[get_db] = _override_db
    api_app.dependency_overrides[get_settings] = lambda: Settings(
        admin_token="test-admin-token", webhook_secret=WEBHOOK_SECRET
    )
    try:
        asyncio.run(_baseline_deploy_degrade())

        deployment = db.scalar(select(Deployment).where(Deployment.external_id == "deploy-1"))
        eval_now = deployment.effective_deployed_at + timedelta(minutes=5, seconds=1)

        # Auto-evaluation finds the due deploy and the detector flags the regression.
        evaluated = evaluate_due_deployments(factory, defaults=TEST_THRESHOLDS, now=eval_now)
        assert deployment.id in evaluated
        db.expire_all()
        deployment = db.get(Deployment, deployment.id)
        assert deployment.evaluation_status == "evaluated_regression"
        assert _count(db, Incident) == 1

        # The incident produces exactly one tracked alert.
        sender = FakeAlertSender()
        assert asyncio.run(dispatch_pending_alerts(db, sender)) == 1
        assert len(sender.calls) == 1
        assert db.scalar(select(Alert)).status == "sent"

        # Resending the same deploy event is a no-op everywhere.
        assert asyncio.run(_resend_webhook()) == 200
        assert _count(db, Deployment) == 1

        assert evaluate_due_deployments(factory, defaults=TEST_THRESHOLDS, now=eval_now) == []
        assert _count(db, Incident) == 1

        resender = FakeAlertSender()
        assert asyncio.run(dispatch_pending_alerts(db, resender)) == 0
        assert resender.calls == []
        assert _count(db, Alert) == 1
    finally:
        api_app.dependency_overrides.clear()
