"""Integration test: check_and_record writes a checks row against Postgres."""

from __future__ import annotations

import asyncio
import ipaddress

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from releasepulse.models import Check, Endpoint, Service
from releasepulse.worker.runner import (
    ALERT_JOB_ID,
    EVALUATE_JOB_ID,
    RECONCILE_JOB_ID,
    check_and_record,
    reconcile,
)


def _noop() -> None:
    pass


def _public_resolver(host, port):
    return [ipaddress.ip_address("93.184.216.34")]


def _endpoint(db) -> Endpoint:
    svc = Service(name="svc")
    db.add(svc)
    db.flush()
    ep = Endpoint(
        service_id=svc.id,
        url="https://svc.example/health",
        method="GET",
        expected_status=200,
        timeout_sec=5,
        check_interval_sec=30,
    )
    db.add(ep)
    db.commit()
    return ep


def _record(db, endpoint, handler):
    async def go():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            return await check_and_record(
                db, endpoint, client,
                app_env="production", allowlist_raw="", resolver=_public_resolver,
            )

    return asyncio.run(go())


def test_records_successful_check(db) -> None:
    ep = _endpoint(db)
    check = _record(db, ep, lambda req: httpx.Response(200))

    assert check.success is True
    rows = db.scalars(select(Check).where(Check.endpoint_id == ep.id)).all()
    assert len(rows) == 1
    assert rows[0].success is True
    assert rows[0].status_code == 200
    assert rows[0].latency_ms is not None
    assert rows[0].error_type is None


def test_records_failed_check(db) -> None:
    ep = _endpoint(db)
    check = _record(db, ep, lambda req: httpx.Response(503))

    assert check.success is False
    row = db.scalar(select(Check).where(Check.endpoint_id == ep.id))
    assert row.success is False
    assert row.status_code == 503
    assert row.error_type == "unexpected_status"


def test_reconcile_preserves_periodic_jobs(db, engine) -> None:
    """reconcile manages only endpoint jobs; it must never sweep up the periodic
    jobs (evaluate_due, dispatch_alerts) that share the scheduler."""
    scheduler = AsyncIOScheduler()
    for job_id in (RECONCILE_JOB_ID, EVALUATE_JOB_ID, ALERT_JOB_ID):
        scheduler.add_job(_noop, IntervalTrigger(seconds=30), id=job_id)
    # A stray job standing in for a since-removed endpoint - this one should go.
    scheduler.add_job(_noop, IntervalTrigger(seconds=30), id="stale-endpoint")

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    reconcile(scheduler, factory, None, "production", "")  # no endpoints in the DB

    ids = {j.id for j in scheduler.get_jobs()}
    assert EVALUATE_JOB_ID in ids
    assert ALERT_JOB_ID in ids
    assert RECONCILE_JOB_ID in ids
    assert "stale-endpoint" not in ids
