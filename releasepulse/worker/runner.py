"""The check worker: schedule per-endpoint HTTP checks and persist results.

Single replica (per spec). APScheduler fires one job per enabled endpoint at its
interval; a periodic reconcile keeps the job set in sync with the database so
newly registered or removed endpoints are picked up without a restart.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import UUID

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from releasepulse.config import get_settings
from releasepulse.db import get_sessionmaker
from releasepulse.detector.service import evaluate_due_deployments
from releasepulse.models import Check, Endpoint
from releasepulse.security.ssrf import Resolver, resolve_host
from releasepulse.worker.check import perform_check

logger = logging.getLogger(__name__)

RECONCILE_JOB_ID = "reconcile"
RECONCILE_INTERVAL_SEC = 60
EVALUATE_JOB_ID = "evaluate_due"
EVALUATE_INTERVAL_SEC = 30


async def check_and_record(
    session: Session,
    endpoint: Endpoint,
    client: httpx.AsyncClient,
    *,
    app_env: str,
    allowlist_raw: str,
    resolver: Resolver = resolve_host,
) -> Check:
    """Run one check and persist it as a checks row. Returns the Check."""
    result = await perform_check(
        endpoint, client, app_env=app_env, allowlist_raw=allowlist_raw, resolver=resolver
    )
    check = Check(
        endpoint_id=endpoint.id,
        checked_at=datetime.now(timezone.utc),
        success=result.success,
        status_code=result.status_code,
        latency_ms=result.latency_ms,
        error_type=result.error_type,
        error_detail=result.error_detail,
    )
    session.add(check)
    session.commit()
    return check


async def _run_check(
    endpoint_id: UUID,
    client: httpx.AsyncClient,
    session_factory: sessionmaker[Session],
    app_env: str,
    allowlist_raw: str,
) -> None:
    """Scheduler job: load the endpoint, check it, record the result."""
    with session_factory() as session:
        endpoint = session.get(Endpoint, endpoint_id)
        if endpoint is None or not endpoint.enabled or endpoint.deleted_at is not None:
            return  # registration changed since scheduling; the next reconcile will drop it
        check = await check_and_record(
            session, endpoint, client, app_env=app_env, allowlist_raw=allowlist_raw
        )
        logger.info(
            "checked %s success=%s status=%s latency=%sms error=%s",
            endpoint.url, check.success, check.status_code, check.latency_ms, check.error_type,
        )


def reconcile(
    scheduler: AsyncIOScheduler,
    session_factory: sessionmaker[Session],
    client: httpx.AsyncClient,
    app_env: str,
    allowlist_raw: str,
) -> None:
    """Add jobs for newly enabled endpoints and remove jobs for gone ones.

    Leaves existing jobs untouched so their check cadence isn't disturbed.
    """
    with session_factory() as session:
        endpoints = session.scalars(
            select(Endpoint).where(
                Endpoint.enabled.is_(True), Endpoint.deleted_at.is_(None)
            )
        ).all()
        wanted = {str(e.id): (e.id, e.check_interval_sec) for e in endpoints}

    existing = {j.id for j in scheduler.get_jobs() if j.id != RECONCILE_JOB_ID}

    for job_id, (endpoint_id, interval) in wanted.items():
        if job_id not in existing:
            scheduler.add_job(
                _run_check,
                IntervalTrigger(seconds=interval),
                args=[endpoint_id, client, session_factory, app_env, allowlist_raw],
                id=job_id,
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc),  # check immediately, then on interval
            )
            logger.info("scheduled checks for endpoint %s every %ss", job_id, interval)

    for job_id in existing - wanted.keys():
        scheduler.remove_job(job_id)
        logger.info("unscheduled endpoint %s", job_id)


def evaluate_due(session_factory: sessionmaker[Session]) -> None:
    """Scheduler job: evaluate every deployment whose window has closed."""
    evaluated = evaluate_due_deployments(session_factory)
    if evaluated:
        logger.info("auto-evaluated %d due deployment(s)", len(evaluated))


async def main() -> None:
    settings = get_settings()
    session_factory = get_sessionmaker()
    scheduler = AsyncIOScheduler()

    async with httpx.AsyncClient(follow_redirects=False) as client:
        scheduler.add_job(
            reconcile,
            IntervalTrigger(seconds=RECONCILE_INTERVAL_SEC),
            args=[scheduler, session_factory, client, settings.app_env, settings.ssrf_allowlist],
            id=RECONCILE_JOB_ID,
        )
        reconcile(scheduler, session_factory, client, settings.app_env, settings.ssrf_allowlist)
        scheduler.add_job(
            evaluate_due,
            IntervalTrigger(seconds=EVALUATE_INTERVAL_SEC),
            args=[session_factory],
            id=EVALUATE_JOB_ID,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        # Exclude the two periodic jobs (reconcile + evaluate_due) from the count.
        logger.info("worker started with %d endpoint job(s)", len(scheduler.get_jobs()) - 2)
        try:
            await asyncio.Event().wait()  # run until interrupted
        finally:
            scheduler.shutdown(wait=False)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())


if __name__ == "__main__":
    run()
